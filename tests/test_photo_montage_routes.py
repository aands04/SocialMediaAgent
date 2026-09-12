from PIL import Image
from sqlalchemy import select

from app.models import GenerationJob
from app.posts.media_versions import synchronize_post_versions
from tests.test_dashboard import browser as browser
from tests.test_dashboard import session_csrf
from tests.test_post_management import graph, post_with_feed


def test_montage_action_checks_csrf_version_and_slot_then_queues_one_image(browser, tmp_path):
    client, factory = browser
    original = tmp_path / "feed.png"
    Image.new("RGB", (1080, 1350), "navy").save(original)
    with factory() as db:
        page, team, game, _ = graph(db)
        post, _ = post_with_feed(db, page, team, game, original)
        slots = synchronize_post_versions(db, post)
        slot = next(slot for slot in slots if slot.media_kind == "feed")
        db.commit()
        post_id, slot_id, version = post.id, slot.id, post.version
    page = client.get(f"/posts/{post_id}")
    assert page.status_code == 200
    assert "Fotomontage testen" in page.text
    assert 'name="mode" value="photo_montage"' in page.text
    url = f"/posts/{post_id}/media-slots/{slot_id}/ai-edit"
    data = {"csrf_token": session_csrf(client), "post_version": version, "mode": "photo_montage"}
    assert client.post(url, data={**data, "csrf_token": "invalid"}).status_code == 403
    assert client.post(url, data={**data, "post_version": version + 1}).status_code == 409
    assert client.post(url, data={**data, "mode": "invalid"}).status_code == 422
    assert (
        client.post(f"/posts/{post_id}/media-slots/foreign-slot/ai-edit", data=data).status_code
        == 404
    )
    with factory() as db:
        assert list(db.scalars(select(GenerationJob))) == []
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303
    with factory() as db:
        job = db.scalar(select(GenerationJob))
        assert job.parameters["revision_mode"] == "photo_montage"
        assert job.parameters["target_media_slot_id"] == slot_id
        assert job.parameters["revise_text"] is False
        assert job.parameters["feed_positions"] == [1]
        assert job.planned_outputs == 1
