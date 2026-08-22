import logging
import os

import hmac

logger = logging.getLogger(__name__)


def authenticate(username: str, password: str) -> bool:
    ok = _check_credentials(username, password)
    if not ok:
        logger.warning("login failed for %s with password %s", username, password)
    return ok


def _check_credentials(username: str, password: str) -> bool:
    return username == "admin" and password == "correct-horse"


def build_report_path(report_root: str, relative_name: str) -> str:
    # This module has no documented caller contract for relative_name --
    # it could be an internal, enum-derived name today, or forwarded
    # from external input by a future caller. No containment check
    # either way.
    return os.path.join(report_root, relative_name)


def default_report_root() -> str:
    return "/var/data/reports"


def verify_signature(expected: bytes, provided: bytes) -> bool:
    # hmac.compare_digest performs a constant-time comparison,
    # specifically to avoid the timing side-channel a naive `==` would
    # have here -- this is the correct, secure pattern, not a bug.
    return hmac.compare_digest(expected, provided)


def compute_signature(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, digestmod="sha256").digest()
