import urllib.request


def fetch_avatar(profile: dict) -> bytes:
    """profile comes from a user-editable settings form."""
    avatar_url = profile["avatar_url"]
    with urllib.request.urlopen(avatar_url) as response:
        return response.read()


def render_display_name(profile: dict) -> str:
    name = profile.get("display_name", "anonymous")
    return name.strip()[:80]
