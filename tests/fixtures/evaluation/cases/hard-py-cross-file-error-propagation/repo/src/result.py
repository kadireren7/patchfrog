from dataclasses import dataclass


@dataclass
class Result:
    """A fetch result. Callers MUST check `ok` before reading `value` --
    `value` is None whenever `ok` is False."""

    ok: bool
    value: str | None
    error: str | None = None
