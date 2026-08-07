from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.approvals.service import approve, edit_text
from app.models import (
    ContentRuleSet,
    Game,
    GeneratedMediaSlot,
    GeneratedMediaVersion,
    InstagramPage,
    JobStatus,
    PostStatus,
    PostTextVersion,
    PublicationJob,
    PublicationMediaItem,
    PublicationRuleSlot,
    Role,
    StoryRule,
    Team,
    UsageLedgerEntry,
    User,
)
from app.posts.media_versions import (
    MediaVersionError,
    ensure_text_version,
    select_media_version,
    select_publication_media_variant,
    select_text_version,
)
from app.posts.rules import calculate_publication_time, resolve_publication_slots
from app.posts.service import create_post, rerender_post, reschedule_game
from app.publishing.schedule import reschedule_publication_job
from app.rendering.service import Renderer
from app.textgen.service import FixtureTextGenerator


def _graph(db, *, kickoff=None, rules=None):
    page = InstagramPage(
        internal_name="main",
        display_name="Hauptseite",
        username="club",
        club="Testverein",
        active=True,
        connection_status="connected",
        publishing_enabled=True,
        account_id="42",
    )
    db.add(page)
    db.flush()
    team = Team(
        internal_name="one",
        display_name="Erste",
        short_name="I",
        slug="erste",
        club="Testverein",
        fussball_url="https://www.fussball.de/x",
        instagram_page_id=page.id,
        media_subdir="erste",
        rules=rules or {"feed_before_minutes": 60},
    )
    db.add(team)
    db.flush()
    game = Game(
        team_id=team.id,
        external_id="g1",
        home_team="Testverein",
        away_team="FC Beispiel",
        kickoff=kickoff or datetime.now(timezone.utc) + timedelta(hours=5),
        source_url=team.fussball_url,
    )
    db.add(game)
    db.commit()
    return page, team, game


def _post_with_two_feed_outputs(db, tmp_path):
    _page, team, game = _graph(
        db,
        rules={
            "feed_before_minutes": 60,
            "announcement_feed_output_count": 2,
            "announcement_story_output_count": 0,
        },
    )
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    return team, game, post


def test_targeted_feed_regeneration_versions_only_selected_slot(db, tmp_path):
    _team, _game, post = _post_with_two_feed_outputs(db, tmp_path)
    slots = list(
        db.scalars(
            select(GeneratedMediaSlot)
            .where(GeneratedMediaSlot.post_id == post.id, GeneratedMediaSlot.media_kind == "feed")
            .order_by(GeneratedMediaSlot.output_position)
        )
    )
    first_selected = slots[0].selected_version_id
    second_selected = slots[1].selected_version_id

    rerender_post(
        db,
        post,
        Renderer(tmp_path / "out"),
        [],
        rerender_feed=True,
        feed_positions=[2],
    )
    db.commit()

    assert db.scalar(
        select(func.count(GeneratedMediaVersion.id)).where(
            GeneratedMediaVersion.slot_id == slots[0].id
        )
    ) == 1
    assert db.scalar(
        select(func.count(GeneratedMediaVersion.id)).where(
            GeneratedMediaVersion.slot_id == slots[1].id
        )
    ) == 2
    assert slots[0].selected_version_id == first_selected
    assert slots[1].selected_version_id != second_selected
    assert slots[1].selected_version_id == slots[1].latest_version_id


def test_manual_media_selection_survives_later_generation_and_revokes_approval(db, tmp_path):
    _team, _game, post = _post_with_two_feed_outputs(db, tmp_path)
    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.media_kind == "feed",
            GeneratedMediaSlot.output_position == 2,
        )
    )
    original = db.get(GeneratedMediaVersion, slot.selected_version_id)
    post.status = PostStatus.APPROVED
    post.approved_version = post.version
    for job in db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id)):
        job.status = JobStatus.SCHEDULED
        job.approval_status = "approved"
        job.approved_post_version = post.version
    db.commit()

    select_media_version(db, post, slot.id, original.id)
    db.commit()
    assert slot.selection_mode == "manual"
    assert post.status == PostStatus.REAPPROVAL
    assert post.approved_version is None

    rerender_post(
        db,
        post,
        Renderer(tmp_path / "out"),
        [],
        rerender_feed=True,
        feed_positions=[2],
    )
    db.commit()
    assert slot.latest_version_id != original.id
    assert slot.selected_version_id == original.id
    carousel = db.scalar(
        select(PublicationJob).where(PublicationJob.post_id == post.id)
    )
    second_item = db.scalar(
        select(PublicationMediaItem).where(
            PublicationMediaItem.publication_job_id == carousel.id,
            PublicationMediaItem.position == 2,
        )
    )
    assert second_item.media_version_id == original.id
    assert second_item.media_path == original.media_path


def test_approval_freezes_selected_media_and_text_versions(db, tmp_path):
    _team, _game, post = _post_with_two_feed_outputs(db, tmp_path)
    user = User(
        email="freigabe@example.org",
        password_hash="x",
        role=Role.APPROVER,
        all_teams=True,
    )
    db.add(user)
    post.critical_warnings = []
    snapshot = dict(post.design_snapshot or {})
    snapshot["logos"] = {
        "team": {"id": "verified", "version": 1, "checksum": "0" * 64, "verified": True}
    }
    post.design_snapshot = snapshot
    db.commit()

    slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.output_position == 2,
        )
    )
    selected_media = db.get(GeneratedMediaVersion, slot.selected_version_id)
    selected_text = db.get(PostTextVersion, post.selected_text_version_id)
    approve(db, post, user)

    job = db.scalar(select(PublicationJob).where(PublicationJob.post_id == post.id))
    media = db.scalar(
        select(PublicationMediaItem).where(
            PublicationMediaItem.publication_job_id == job.id,
            PublicationMediaItem.position == 2,
        )
    )
    assert post.approved_version == post.version
    assert job.text_version_id == selected_text.id
    assert job.text_snapshot == selected_text.text
    assert media.media_version_id == selected_media.id
    assert media.media_path == selected_media.media_path


def test_historical_media_and_text_versions_are_immutable(db, tmp_path):
    _team, _game, post = _post_with_two_feed_outputs(db, tmp_path)
    slot = db.scalar(
        select(GeneratedMediaSlot).where(GeneratedMediaSlot.post_id == post.id)
    )
    media = db.get(GeneratedMediaVersion, slot.selected_version_id)
    text_version = db.get(PostTextVersion, post.selected_text_version_id)

    media.checksum = "f" * 64
    with pytest.raises(PermissionError, match="unveränderlich"):
        db.commit()
    db.rollback()

    text_version = db.get(PostTextVersion, post.selected_text_version_id)
    text_version.text = "Nachträglich manipuliert"
    with pytest.raises(PermissionError, match="unveränderlich"):
        db.commit()
    db.rollback()


def test_foreign_post_variant_is_rejected_without_usage(db, tmp_path):
    _page, team, first_game = _graph(db)
    second_game = Game(
        team_id=team.id,
        external_id="g2",
        home_team="Testverein",
        away_team="FC Zweites Beispiel",
        kickoff=first_game.kickoff + timedelta(days=7),
        source_url=team.fussball_url,
    )
    db.add(second_game)
    db.commit()
    renderer = Renderer(tmp_path / "out")
    first_post = create_post(db, first_game, team, FixtureTextGenerator(), renderer)
    second_post = create_post(db, second_game, team, FixtureTextGenerator(), renderer)
    first_job = db.scalar(
        select(PublicationJob).where(PublicationJob.post_id == first_post.id)
    )
    foreign_slot = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.post_id == second_post.id,
            GeneratedMediaSlot.media_kind == "feed",
        )
    )
    usage_before = db.scalar(select(func.count(UsageLedgerEntry.id))) or 0

    with pytest.raises(MediaVersionError, match="gehört nicht"):
        select_publication_media_variant(
            db,
            first_post,
            publication_job_id=first_job.id,
            slot_id=foreign_slot.id,
        )

    assert (db.scalar(select(func.count(UsageLedgerEntry.id))) or 0) == usage_before


def test_manual_text_selection_survives_new_generated_text(db, tmp_path):
    _team, _game, post = _post_with_two_feed_outputs(db, tmp_path)
    user = User(email="redaktion@example.org", password_hash="x", role=Role.EDITOR, all_teams=True)
    db.add(user)
    db.commit()
    first = db.get(PostTextVersion, post.selected_text_version_id)
    edit_text(db, post, user, "Zweite, manuell bearbeitete Fassung", post.version)
    second = db.get(PostTextVersion, post.selected_text_version_id)
    assert second.id != first.id

    select_text_version(db, post, first.id)
    post.text = "Neue KI-Fassung, die noch nicht ausgewählt werden soll"
    third = ensure_text_version(db, post, source="generation")
    db.commit()

    assert third.version_number == 3
    assert post.latest_text_version_id == third.id
    assert post.selected_text_version_id == first.id
    assert post.text == first.text
    assert post.text_selection_mode == "manual"


def test_missing_weekday_rule_requires_manual_schedule_without_fallback(db, tmp_path):
    kickoff = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)  # Montag
    _page, team, game = _graph(
        db,
        kickoff=kickoff,
        rules={
            "announcement_timing_mode": "weekday_fixed",
            "announcement_weekday_times": {"6": "18:00"},
            "announcement_weekday_targets": {"6": "4"},
            "announcement_feed_output_count": 1,
            "announcement_story_output_count": 0,
        },
    )
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    job = db.scalar(select(PublicationJob).where(PublicationJob.post_id == post.id))
    assert job.approval_status == "manual_schedule_required"
    assert job.status == JobStatus.DRAFT
    assert job.schedule_source == "manual_required"
    assert not any("Wochentag" in warning for warning in post.critical_warnings)

    user = User(email="planung@example.org", password_hash="x", role=Role.EDITOR, all_teams=True)
    db.add(user)
    db.commit()
    reschedule_publication_job(
        db,
        post=post,
        job=job,
        user=user,
        scheduled_at=datetime.now(timezone.utc) + timedelta(days=3),
        expected_job_version=job.version,
    )
    assert job.approval_status == "unapproved"
    assert job.status == JobStatus.UNAPPROVED
    assert job.schedule_source == "manual"


def test_rule_resolution_prefers_game_then_team_then_club_and_never_falls_back(db):
    _page, team, game = _graph(
        db, kickoff=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    )
    club_id = team.club_id
    club_rule = ContentRuleSet(
        club_id=club_id,
        scope_type="club",
        scope_key="club",
        post_type="announcement",
        rule_version=1,
    )
    team_rule = ContentRuleSet(
        club_id=club_id,
        scope_type="team",
        scope_key=f"team:{team.id}",
        team_id=team.id,
        post_type="announcement",
        rule_version=1,
    )
    game_rule = ContentRuleSet(
        club_id=club_id,
        scope_type="game",
        scope_key=f"game:{game.id}",
        game_id=game.id,
        post_type="announcement",
        rule_version=1,
    )
    db.add_all([club_rule, team_rule, game_rule])
    db.flush()
    db.add(
        PublicationRuleSlot(
            club_id=club_id,
            rule_set_id=game_rule.id,
            slot_key="feed:sunday",
            label="Nur Sonntag",
            media_kind="feed",
            variant_number=1,
            timing_model="weekday_fixed",
            reference="kickoff",
            match_weekday=6,
            target_weekday=4,
            local_time="18:00",
        )
    )
    db.commit()

    monday = resolve_publication_slots(
        db,
        club_id=club_id,
        post_type="announcement",
        team_id=team.id,
        game_id=game.id,
        match_weekday=0,
    )
    assert monday.rule_set.id == game_rule.id
    assert monday.source == "game:v1"
    assert monday.slots == ()
    assert monday.manual_schedule_required

    sunday = resolve_publication_slots(
        db,
        club_id=club_id,
        post_type="announcement",
        team_id=team.id,
        game_id=game.id,
        match_weekday=6,
    )
    assert len(sunday.slots) == 1
    assert calculate_publication_time(sunday.slots[0], game) == datetime(
        2026, 8, 7, 16, 0, tzinfo=timezone.utc
    )


def test_story_alternative_without_publication_job_can_be_regenerated(db, tmp_path):
    _page, team, game = _graph(
        db,
        rules={
            "announcement_feed_generation_count": 1,
            "announcement_feed_publish_count": 1,
            "announcement_story_generation_count": 2,
            "announcement_story_publish_count": 1,
        },
    )
    rule = StoryRule(
        team_id=team.id,
        name="Ankündigung",
        post_type="announcement",
        reference="kickoff",
        direction="before",
        offset_minutes=60,
        template="default-story",
        prompt_template="default-image-story",
        media_slot=1,
        active=True,
    )
    db.add(rule)
    db.commit()
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    alternative = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.media_kind == "story",
            GeneratedMediaSlot.variant_number == 2,
        )
    )
    assert alternative
    assert db.scalar(
        select(func.count(PublicationJob.id)).where(
            PublicationJob.post_id == post.id,
            PublicationJob.kind == "story",
        )
    ) == 1

    rerender_post(
        db,
        post,
        Renderer(tmp_path / "out"),
        [],
        rerender_feed=False,
        story_variant_numbers=[2],
    )
    db.commit()

    assert db.scalar(
        select(func.count(GeneratedMediaVersion.id)).where(
            GeneratedMediaVersion.slot_id == alternative.id
        )
    ) == 2
    assert alternative.latest_version_id != alternative.selected_version_id or (
        alternative.selection_mode == "auto_latest"
        and alternative.latest_version_id == alternative.selected_version_id
    )


def test_existing_feed_alternative_can_be_assigned_without_ai_usage(db, tmp_path):
    _page, team, game = _graph(
        db,
        rules={
            "announcement_feed_generation_count": 2,
            "announcement_feed_publish_count": 1,
            "announcement_story_generation_count": 0,
            "announcement_story_publish_count": 0,
        },
    )
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    job = db.scalar(select(PublicationJob).where(PublicationJob.post_id == post.id))
    alternative = db.scalar(
        select(GeneratedMediaSlot).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.media_kind == "feed",
            GeneratedMediaSlot.variant_number == 2,
        )
    )
    initial_usage = db.scalar(select(func.count(UsageLedgerEntry.id))) or 0

    selected = select_publication_media_variant(
        db,
        post,
        publication_job_id=job.id,
        slot_id=alternative.id,
    )
    db.commit()

    assert job.media_version_id == selected.id
    assert job.media_path == selected.media_path
    assert job.status == JobStatus.UNAPPROVED
    assert job.approval_status == "reapproval_required"
    assert (db.scalar(select(func.count(UsageLedgerEntry.id))) or 0) == initial_usage


def _weekday_rule_set(db, team, game):
    rule_set = ContentRuleSet(
        club_id=team.club_id,
        scope_type="team",
        scope_key=f"team:{team.id}",
        team_id=team.id,
        post_type="announcement",
        rule_version=1,
        feed_generation_count=1,
        story_generation_count=0,
    )
    db.add(rule_set)
    db.flush()
    sunday = PublicationRuleSlot(
        club_id=team.club_id,
        rule_set_id=rule_set.id,
        slot_key="feed:sunday",
        label="Sonntagsspiel",
        media_kind="feed",
        variant_number=1,
        timing_model="weekday_fixed",
        reference="kickoff",
        match_weekday=6,
        target_weekday=4,
        local_time="18:00",
    )
    saturday = PublicationRuleSlot(
        club_id=team.club_id,
        rule_set_id=rule_set.id,
        slot_key="feed:saturday",
        label="Samstagsspiel",
        media_kind="feed",
        variant_number=1,
        timing_model="weekday_fixed",
        reference="kickoff",
        match_weekday=5,
        target_weekday=3,
        local_time="17:00",
    )
    db.add_all([sunday, saturday])
    db.commit()
    return sunday, saturday


def test_reschedule_to_configured_weekday_recalculates_unpublished_slot(db, tmp_path):
    kickoff = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    _page, team, game = _graph(db, kickoff=kickoff)
    _sunday, saturday = _weekday_rule_set(db, team, game)
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    job = db.scalar(select(PublicationJob).where(PublicationJob.post_id == post.id))
    job.status = JobStatus.SCHEDULED
    job.approval_status = "approved"
    post.status = PostStatus.APPROVED
    db.commit()

    reschedule_game(db, game, datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc))

    assert job.publication_rule_slot_id == saturday.id
    assert job.scheduled_at == datetime(2026, 8, 6, 15, 0, tzinfo=timezone.utc)
    assert job.status == JobStatus.UNAPPROVED
    assert post.status == PostStatus.REAPPROVAL


def test_reschedule_to_weekday_without_rule_requires_manual_plan(db, tmp_path):
    kickoff = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    _page, team, game = _graph(db, kickoff=kickoff)
    _weekday_rule_set(db, team, game)
    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    job = db.scalar(select(PublicationJob).where(PublicationJob.post_id == post.id))
    db.commit()

    reschedule_game(db, game, datetime(2026, 8, 12, 13, 0, tzinfo=timezone.utc))

    assert job.status == JobStatus.DRAFT
    assert job.approval_status == "manual_schedule_required"
    assert job.schedule_source == "manual_required"
    assert job.stale_time


def test_structured_rules_create_multiple_feed_and_story_publications(db, tmp_path):
    kickoff = datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc)
    page, team, game = _graph(db, kickoff=kickoff)
    rule_set = ContentRuleSet(
        club_id=team.club_id,
        scope_type="team",
        scope_key=f"team:{team.id}",
        team_id=team.id,
        post_type="announcement",
        rule_version=1,
        feed_generation_count=2,
        story_generation_count=3,
        feed_publish_variants=[1, 2],
        story_publish_variants=[1, 2],
    )
    db.add(rule_set)
    db.flush()
    slots = [
        PublicationRuleSlot(
            club_id=team.club_id,
            rule_set_id=rule_set.id,
            slot_key="feed:first",
            label="Feed am Freitag",
            media_kind="feed",
            variant_number=1,
            timing_model="weekday_fixed",
            reference="kickoff",
            match_weekday=6,
            target_weekday=4,
            local_time="18:00",
            instagram_page_id=page.id,
        ),
        PublicationRuleSlot(
            club_id=team.club_id,
            rule_set_id=rule_set.id,
            slot_key="feed:second",
            label="Alternativer Feed",
            media_kind="feed",
            variant_number=2,
            timing_model="relative",
            reference="kickoff",
            direction="before",
            offset_minutes=60,
            instagram_page_id=page.id,
        ),
        PublicationRuleSlot(
            club_id=team.club_id,
            rule_set_id=rule_set.id,
            slot_key="story:first",
            label="Story am Samstag",
            media_kind="story",
            variant_number=1,
            timing_model="relative",
            reference="kickoff",
            direction="before",
            offset_minutes=120,
            instagram_page_id=page.id,
        ),
        PublicationRuleSlot(
            club_id=team.club_id,
            rule_set_id=rule_set.id,
            slot_key="story:reuse",
            label="Story erneut",
            media_kind="story",
            variant_number=1,
            timing_model="relative",
            reference="kickoff",
            direction="before",
            offset_minutes=30,
            instagram_page_id=page.id,
            reuse_media=True,
        ),
    ]
    db.add_all(slots)
    db.commit()

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    jobs = list(
        db.scalars(
            select(PublicationJob)
            .where(PublicationJob.post_id == post.id)
            .order_by(PublicationJob.scheduled_at)
        )
    )

    assert len(jobs) == 4
    assert [job.kind for job in jobs].count("feed") == 2
    assert [job.kind for job in jobs].count("story") == 2
    assert {job.publication_rule_slot_id for job in jobs} == {slot.id for slot in slots}
    assert all(job.status == JobStatus.UNAPPROVED for job in jobs)
    assert db.scalar(
        select(func.count(GeneratedMediaSlot.id)).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.media_kind == "feed",
        )
    ) == 2
    assert db.scalar(
        select(func.count(GeneratedMediaSlot.id)).where(
            GeneratedMediaSlot.post_id == post.id,
            GeneratedMediaSlot.media_kind == "story",
        )
    ) == 3
    story_jobs = [job for job in jobs if job.kind == "story"]
    assert story_jobs[0].media_path == story_jobs[1].media_path


def test_structured_rule_without_matching_weekday_keeps_variants_and_requires_planning(
    db, tmp_path
):
    kickoff = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)  # Montag
    _page, team, game = _graph(db, kickoff=kickoff)
    rule_set = ContentRuleSet(
        club_id=team.club_id,
        scope_type="team",
        scope_key=f"team:{team.id}",
        team_id=team.id,
        post_type="announcement",
        rule_version=1,
        feed_generation_count=2,
        story_generation_count=2,
        feed_publish_variants=[1],
        story_publish_variants=[1, 2],
    )
    db.add(rule_set)
    db.flush()
    db.add(
        PublicationRuleSlot(
            club_id=team.club_id,
            rule_set_id=rule_set.id,
            slot_key="feed:sunday",
            label="Nur Sonntag",
            media_kind="feed",
            variant_number=1,
            timing_model="weekday_fixed",
            reference="kickoff",
            match_weekday=6,
            target_weekday=4,
            local_time="18:00",
        )
    )
    db.commit()

    post = create_post(
        db,
        game,
        team,
        FixtureTextGenerator(),
        Renderer(tmp_path / "out"),
    )
    jobs = list(db.scalars(select(PublicationJob).where(PublicationJob.post_id == post.id)))

    assert len(jobs) == 3
    assert [job.kind for job in jobs].count("feed") == 1
    assert [job.kind for job in jobs].count("story") == 2
    assert all(job.status == JobStatus.DRAFT for job in jobs)
    assert all(job.approval_status == "manual_schedule_required" for job in jobs)
    assert all(job.publication_rule_slot_id is None for job in jobs)
