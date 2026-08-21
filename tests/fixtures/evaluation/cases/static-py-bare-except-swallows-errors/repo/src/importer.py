import json


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except:
        # Swallows everything, including KeyboardInterrupt and
        # SystemExit, and hides real parsing/IO errors as if the config
        # were simply absent -- ruff's default E722 (bare except) rule
        # catches exactly this pattern.
        return {}


def load_defaults() -> dict:
    return {"timeout": 30}
