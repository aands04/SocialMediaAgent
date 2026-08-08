from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.file_delivery import detached_file_response


class RecordingSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_detached_file_response_releases_session_before_delivery(tmp_path: Path):
    path = tmp_path / "preview.png"
    path.write_bytes(b"test-image")
    db = RecordingSession()

    response = detached_file_response(db, path, media_type="image/png")

    assert db.closed is True
    assert Path(response.path) == path
    assert response.media_type == "image/png"


def test_file_transfer_does_not_retain_the_only_pool_connection(tmp_path: Path):
    path = tmp_path / "large-preview.png"
    path.write_bytes(b"preview-body")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    first = Session(engine)
    first.execute(text("SELECT 1"))

    response = detached_file_response(first, path, media_type="image/png")

    # The response has not been delivered yet. A second request must still be
    # able to use the only pool connection immediately.
    with Session(engine) as second:
        assert second.scalar(text("SELECT 1")) == 1
    assert Path(response.path) == path
    engine.dispose()


def test_detached_file_response_releases_session_when_response_creation_fails(
    monkeypatch, tmp_path: Path
):
    db = RecordingSession()

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr("app.file_delivery.FileResponse", fail_response)

    with pytest.raises(RuntimeError, match="response construction failed"):
        detached_file_response(db, tmp_path / "missing.png", media_type="image/png")

    assert db.closed is True
