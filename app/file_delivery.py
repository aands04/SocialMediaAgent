from os import PathLike

from fastapi.responses import FileResponse
from sqlalchemy.orm import Session


def detached_file_response(
    db: Session,
    path: str | PathLike[str],
    *,
    media_type: str | None = None,
    filename: str | None = None,
    content_disposition_type: str = "attachment",
    headers: dict[str, str] | None = None,
) -> FileResponse:
    """Build a file response without retaining a database connection while streaming.

    FastAPI may keep yield-based dependencies alive until a response has been
    handled completely. A browser can request many media files concurrently, so
    retaining one SQLAlchemy connection per transfer can exhaust the connection
    pool. All authorization and path validation must be completed before calling
    this helper.
    """

    try:
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            content_disposition_type=content_disposition_type,
            headers=headers,
        )
    finally:
        # Session.close() is idempotent. The get_db dependency will close the
        # session again during teardown, but the checked-out connection is
        # already available to other requests before file transfer starts.
        db.close()
