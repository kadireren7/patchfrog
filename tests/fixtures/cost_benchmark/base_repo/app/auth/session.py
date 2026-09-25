"""Session handling."""

import time


def issue_session(user_id, ttl_seconds):
    """Create a session record valid for ttl_seconds."""
    return {"user": user_id, "expires_at": time.time() + ttl_seconds}


def is_valid(session, now=None):
    """True while the session has not expired."""
    current = time.time() if now is None else now
    return current < session["expires_at"]
