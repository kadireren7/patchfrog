import os

_UPLOADS_DIR = "/var/data/uploads"


def read_upload(filename: str) -> bytes:
    """Read a previously uploaded file by name."""
    full_path = os.path.join(_UPLOADS_DIR, filename)
    with open(full_path, "rb") as handle:
        return handle.read()


def list_upload_names() -> list[str]:
    return sorted(os.listdir(_UPLOADS_DIR))
