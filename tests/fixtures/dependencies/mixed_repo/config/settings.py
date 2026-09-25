"""Runtime settings -- values come from the environment, never the repo."""

import os

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
