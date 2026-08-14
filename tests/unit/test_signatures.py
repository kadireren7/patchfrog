from __future__ import annotations

import hashlib
import hmac

from patchfrog.github.signatures import verify_signature

SECRET = "my-webhook-secret"
PAYLOAD = b'{"action": "opened", "number": 14}'


def _sign(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_is_accepted() -> None:
    signature = _sign(SECRET, PAYLOAD)
    assert verify_signature(secret=SECRET, payload=PAYLOAD, signature_header=signature) is True


def test_invalid_signature_is_rejected() -> None:
    wrong_signature = _sign("a-completely-different-secret", PAYLOAD)
    assert (
        verify_signature(secret=SECRET, payload=PAYLOAD, signature_header=wrong_signature)
        is False
    )


def test_missing_signature_is_rejected() -> None:
    assert verify_signature(secret=SECRET, payload=PAYLOAD, signature_header=None) is False


def test_tampered_payload_is_rejected() -> None:
    signature = _sign(SECRET, PAYLOAD)
    tampered_payload = PAYLOAD.replace(b"opened", b"closed")
    assert (
        verify_signature(secret=SECRET, payload=tampered_payload, signature_header=signature)
        is False
    )


def test_signature_missing_prefix_is_rejected() -> None:
    digest = hmac.new(SECRET.encode("utf-8"), PAYLOAD, hashlib.sha256).hexdigest()
    assert verify_signature(secret=SECRET, payload=PAYLOAD, signature_header=digest) is False


def test_empty_signature_header_is_rejected() -> None:
    assert verify_signature(secret=SECRET, payload=PAYLOAD, signature_header="") is False
