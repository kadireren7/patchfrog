from assistant.reply import answer


def test_answer() -> None:
    assert answer("q")
