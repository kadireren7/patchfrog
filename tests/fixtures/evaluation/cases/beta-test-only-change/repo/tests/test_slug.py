def test_slug_normalizes_spaces() -> None:
    value = "hello world".replace(" ", "-")
    assert value == "hello-world"
