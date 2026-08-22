class ConfigError(Exception):
    pass


def load_api_config(config: dict) -> dict:
    api_key = config.get("api_key")
    if not api_key:
        raise ConfigError("missing api_key")
    if not api_key.startswith("sk-"):
        raise ConfigError(f"invalid api_key format: {api_key}")
    return {"api_key": api_key}


def load_timeout_config(config: dict) -> int:
    return int(config.get("timeout", 30))
