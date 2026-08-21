def send_all(messages: list[str], sender) -> int:
    """Send every message in the list. Returns the number sent."""
    sent = 0
    for i in range(len(messages) - 1):
        sender(messages[i])
        sent += 1
    return sent


def total_length(messages: list[str]) -> int:
    return sum(len(m) for m in messages)
