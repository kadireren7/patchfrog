from src.handler import process_request


def api_route(store, key, value):
    process_request(store, key, value)
