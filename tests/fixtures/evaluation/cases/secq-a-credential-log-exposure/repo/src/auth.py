import logging

logger = logging.getLogger(__name__)


def authenticate(username: str, password: str) -> bool:
    ok = _check_credentials(username, password)
    if not ok:
        logger.warning("login failed for %s with password %s", username, password)
    return ok


def _check_credentials(username: str, password: str) -> bool:
    return username == "admin" and password == "correct-horse"
