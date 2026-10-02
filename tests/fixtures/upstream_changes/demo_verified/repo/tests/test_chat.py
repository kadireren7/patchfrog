from app.ai.chat import generate_reply


def test_generate_reply_is_text() -> None:
    assert isinstance(generate_reply("hi"), str)
