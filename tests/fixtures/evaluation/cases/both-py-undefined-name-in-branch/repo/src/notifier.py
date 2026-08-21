def format_alert(level: str, message: str) -> str:
    if level == "critical":
        # Typo: `mesage` instead of `message` -- undefined name, a real
        # NameError at runtime. Marked ground_truth_source: either, so
        # this counts toward the static/AI overlap matrix's "both"
        # bucket (ruff's F821 rule and the AI oracle both report it).
        return f"CRITICAL: {mesage}"
    return f"{level}: {message}"


def format_footer() -> str:
    return "-- end of alert --"
