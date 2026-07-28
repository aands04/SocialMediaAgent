"""PostgreSQL-only transaction and concurrency tests for staging/CI."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Game, InstagramPage, MediaAsset, Post, PublicationJob, Team
from app.posts.service import reserve_image

URL=os.getenv("TEST_POSTGRES_URL")
pytestmark=pytest.mark.postgresql

@pytest.fixture
def pg():
    if not URL: pytest.skip("TEST_POSTGRES_URL nicht gesetzt")
    engine=create_engine(URL,pool_pre_ping=True)
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine); engine.dispose()

def graph(factory):
    with factory() as db:
        page=InstagramPage(internal_name="pg",display_name="PG",username="pg",club="C",active=True); db.add(page); db.flush()
        team=Team(internal_name="pg",display_name="PG",short_name="PG",slug="pg",club="C",fussball_url="https://www.fussball.de/pg",instagram_page_id=page.id,media_subdir="pg"); db.add(team); db.flush()
        games=[Game(team_id=team.id,external_id=f"g{x}",home_team="A",away_team="B",kickoff=datetime.now(timezone.utc)+timedelta(days=x),source_url="fixture://pg") for x in (1,2)]; db.add_all(games); db.flush()
        asset=MediaAsset(team_id=team.id,relative_path="p.png",filename="p.png",mime_type="image/png",size=1,checksum="x",mtime=datetime.now(timezone.utc)); db.add(asset); db.commit(); return page.id,team.id,[x.id for x in games],asset.id

def test_parallel_image_reservation_uses_postgresql_lock(pg):
    _,team_id,game_ids,asset_id=graph(pg)
    def reserve(game_id):
        with pg() as db:
            result=reserve_image(db,team_id,game_id); db.commit(); return result.id if result else None
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(reserve,game_ids))
    assert results.count(asset_id)==1 and results.count(None)==1
    with pg() as db: assert db.get(MediaAsset,asset_id).uses==1

def test_unique_idempotency_key_and_main_post_constraint(pg):
    page_id,team_id,game_ids,_=graph(pg)
    with pg() as db:
        post=Post(game_id=game_ids[0],team_id=team_id,instagram_page_id=page_id,post_type="announcement"); db.add(post); db.flush()
        base=dict(post_id=post.id,game_id=game_ids[0],team_id=team_id,instagram_page_id=page_id,kind="feed",media_path="x",scheduled_at=datetime.now(timezone.utc),idempotency_key="same-key")
        db.add(PublicationJob(**base)); db.commit()
        db.add(PublicationJob(**base))
        with pytest.raises(IntegrityError): db.commit()
    with pg() as db:
        original=db.scalar(select(Post).where(Post.game_id==game_ids[0])); db.add(Post(game_id=game_ids[0],team_id=team_id,instagram_page_id=page_id,post_type="announcement"))
        with pytest.raises(IntegrityError): db.commit()
        assert original is not None
