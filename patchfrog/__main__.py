"""``python -m patchfrog ...``.

``scale`` commands are dispatched straight to :mod:`patchfrog.cli_scale`,
without importing :mod:`patchfrog.cli`. That module imports the whole review
and compatibility CLI eagerly (database, HTTP and provider libraries;
ROADMAP S3), which local discovery doesn't need. Every other command is
handed to :func:`patchfrog.cli.main` unchanged.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["scale"]:
        from patchfrog import cli_scale

        return cli_scale.main(args[1:])
    from patchfrog import cli

    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
