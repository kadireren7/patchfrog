"""Verification of GitHub webhook HMAC signatures.

GitHub signs every webhook delivery with ``X-Hub-Signature-256``, an
HMAC-SHA256 digest of the raw request body keyed by the configured webhook
secret. This must be verified *before* the payload is deserialized or
otherwise trusted.
"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="


def verify_signature(*, secret: str, payload: bytes, signature_header: str | None) -> bool:
    """Return ``True`` if ``signature_header`` is a valid signature for ``payload``.

    Uses a constant-time comparison to avoid leaking timing information
    about the expected signature.
    """

    if not signature_header:
        return False
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return False

    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_digest, provided_digest)
