from __future__ import annotations

from patchfrog.review.redaction import redact_secrets


def test_pem_private_key_is_redacted() -> None:
    text = (
        "config = {\n"
        "  'key': '-----BEGIN RSA PRIVATE KEY-----\\n"
        "MIIEowIBAAKCAQEA1234567890abcdefgh\\n"
        "-----END RSA PRIVATE KEY-----'\n"
        "}"
    )
    result = redact_secrets(text)
    assert "BEGIN RSA PRIVATE KEY" not in result.text
    assert "MIIEowIBAAKCAQEA1234567890abcdefgh" not in result.text
    assert "[REDACTED]" in result.text
    assert result.redacted_count == 1


def test_github_pat_token_is_redacted() -> None:
    text = 'TOKEN = "ghp_1234567890abcdefghijklmnopqrstuvwx"'
    result = redact_secrets(text)
    assert "ghp_1234567890abcdefghijklmnopqrstuvwx" not in result.text
    assert "[REDACTED]" in result.text


def test_github_fine_grained_pat_is_redacted() -> None:
    text = "auth_header = f'token {github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz}'"
    result = redact_secrets(text)
    assert "github_pat_11ABCDEFG0123456789" not in result.text


def test_aws_access_key_id_is_redacted() -> None:
    text = "AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'"
    result = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text


def test_aws_secret_access_key_is_redacted_but_label_kept() -> None:
    # A 40-char base64-ish placeholder shaped like an AWS secret key, but
    # not AWS's own well-known documentation example (avoids
    # push-protection false-positive flags on a synthetic fixture).
    fake_secret = "A" * 20 + "0123456789" + "b" * 10
    text = f'aws_secret_access_key: "{fake_secret}"'
    result = redact_secrets(text)
    assert fake_secret not in result.text
    assert "aws_secret_access_key" in result.text  # label itself is not secret


def test_labeled_secret_assignment_is_redacted() -> None:
    # Deliberately not a realistic-looking key (no real provider prefix) --
    # GitHub's push-protection secret scanner flags plausible-looking
    # provider keys even in test fixtures; this still exercises the same
    # "LABEL_API_KEY = <long value>" pattern the regex targets.
    fake_key = "NOTAREALKEY0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    text = f'STRIPE_API_KEY = "{fake_key}"'
    result = redact_secrets(text)
    assert fake_key not in result.text
    assert "STRIPE_API_KEY" in result.text


def test_slack_token_is_redacted() -> None:
    # Deliberately not a realistic-looking token (no numeric team/bot ID
    # segments) -- GitHub's push-protection secret scanner flags
    # plausible-looking Slack tokens even in test fixtures.
    fake_token = "xoxb-NOTAREALSLACKTOKEN-FAKEFAKEFAKE-TESTONLY"
    text = f"client = Slack(token='{fake_token}')"
    result = redact_secrets(text)
    assert fake_token not in result.text


def test_ordinary_code_is_never_touched() -> None:
    """The redaction layer must not fire on ordinary code -- a redaction
    layer that mangles normal identifiers is worse than useless."""

    text = (
        "def can_withdraw(balance, amount):\n"
        "    return amount >= balance\n\n"
        "class UserRepository:\n"
        "    def __init__(self, session_factory):\n"
        "        self._session_factory = session_factory\n\n"
        "SOME_CONSTANT_ID = '12345678-1234-1234-1234-123456789012'\n"
        "MAX_RETRIES = 3\n"
    )
    result = redact_secrets(text)
    assert result.text == text
    assert result.redacted_count == 0


def test_short_hex_or_uuid_like_values_are_not_redacted() -> None:
    text = 'commit_sha = "a1b2c3d4e5f6789012345678901234567890abcd"\nid = "12345678-90ab-cdef-1234-567890abcdef"'
    result = redact_secrets(text)
    assert result.text == text


def test_redaction_is_idempotent() -> None:
    text = "TOKEN = 'ghp_1234567890abcdefghijklmnopqrstuvwx'"
    once = redact_secrets(text)
    twice = redact_secrets(once.text)
    assert once.text == twice.text
    assert twice.redacted_count == 0
