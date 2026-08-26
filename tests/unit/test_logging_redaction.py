from __future__ import annotations

from patchfrog.config.logging import _REDACTED, redact_secrets


def _redact(event_dict: dict[str, object]) -> dict[str, object]:
    return dict(redact_secrets(None, "info", event_dict))


def test_secret_shaped_key_names_are_redacted_outright() -> None:
    for key in ("token", "Authorization", "api_key", "webhook_secret", "installation_token", "password"):
        result = _redact({key: "sensitive-value"})
        assert result[key] == _REDACTED


def test_non_secret_keys_pass_through_unchanged() -> None:
    result = _redact({"repository": "kadireren7/libft", "pull_request_number": 14})
    assert result["repository"] == "kadireren7/libft"
    assert result["pull_request_number"] == 14


def test_pem_block_is_redacted_even_under_an_unrelated_key() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END RSA PRIVATE KEY-----"
    result = _redact({"detail": f"failed while loading key: {pem}"})
    assert "BEGIN RSA PRIVATE KEY" not in str(result["detail"])
    assert _REDACTED in str(result["detail"])


def test_bearer_token_embedded_in_free_text_is_redacted() -> None:
    result = _redact({"error": "request failed: Authorization header was Bearer xyz789abc"})
    assert "xyz789abc" not in str(result["error"])
    assert _REDACTED in str(result["error"])


def test_github_token_prefix_is_redacted() -> None:
    result = _redact({"detail": "using token ghp_" + "a" * 36})
    assert "ghp_" not in str(result["detail"])
    assert _REDACTED in str(result["detail"])


def test_non_string_values_pass_through() -> None:
    result = _redact({"count": 5, "enabled": True, "ratio": 1.5})
    assert result == {"count": 5, "enabled": True, "ratio": 1.5}
