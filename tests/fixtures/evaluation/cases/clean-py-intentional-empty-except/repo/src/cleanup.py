import contextlib
import os


def cleanup_temp_file(path: str) -> None:
    # Best-effort cleanup: the file may have already been removed by a
    # concurrent cleanup pass, and that's fine -- there's nothing left
    # to do here.
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def write_marker(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
