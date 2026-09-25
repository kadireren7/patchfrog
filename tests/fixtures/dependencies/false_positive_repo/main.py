import github

# We might call openai or stripe someday; this comment is not a dependency.
STRIPE_COLOR = "stripe"
stripe_width = 3
openai_note = "openai is mentioned in a string only"

DOCS = "https://github.com/acme/project"
LOOKALIKE = "https://api.github.com.evil.example/x"


def link() -> str:
    return github.repo_url("acme", "project")
