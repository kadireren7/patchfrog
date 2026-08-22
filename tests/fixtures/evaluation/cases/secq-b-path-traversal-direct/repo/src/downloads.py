import os

UPLOAD_ROOT = "/var/data/uploads"


def read_uploaded_file(filename: str) -> bytes:
    # filename is taken directly from the HTTP request's query string
    # by the caller (an API route handler), with no validation.
    path = os.path.join(UPLOAD_ROOT, filename)
    with open(path, "rb") as f:
        return f.read()


def list_upload_root() -> list[str]:
    return os.listdir(UPLOAD_ROOT)
