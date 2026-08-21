def parse_amount(raw: str) -> float:
    try:
        return float(raw)
    except:
        # Swallows everything (including a genuine ValueError from
        # malformed input) and silently treats it as zero -- a real,
        # dangerous bug for anything financial. Marked
        # ground_truth_source: either so this exercises the static/AI
        # overlap matrix's "both" bucket (ruff's E722 rule and the AI
        # oracle are both expected to report it).
        return 0.0


def parse_currency_code(raw: str) -> str:
    return raw.strip().upper()
