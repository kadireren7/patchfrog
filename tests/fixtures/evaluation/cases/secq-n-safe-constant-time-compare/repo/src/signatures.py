import hmac


def verify_signature(expected: bytes, provided: bytes) -> bool:
    # hmac.compare_digest performs a constant-time comparison,
    # specifically to avoid the timing side-channel a naive `==` would
    # have here -- this is the correct, secure pattern, not a bug.
    return hmac.compare_digest(expected, provided)


def compute_signature(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, digestmod="sha256").digest()
