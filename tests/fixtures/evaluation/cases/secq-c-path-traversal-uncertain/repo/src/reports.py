import os


def build_report_path(report_root: str, relative_name: str) -> str:
    # This module has no documented caller contract for relative_name --
    # it could be an internal, enum-derived name today, or forwarded
    # from external input by a future caller. No containment check
    # either way.
    return os.path.join(report_root, relative_name)


def default_report_root() -> str:
    return "/var/data/reports"
