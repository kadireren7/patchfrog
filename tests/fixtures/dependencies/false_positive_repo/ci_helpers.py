"""CI helper: reads a token NAME only; no GitHub API call happens here."""

import os

TOKEN = os.environ.get("GITHUB_TOKEN", "")
