RETRY_POLICY_MAX_ATTEMPTS = 5


def compute_backoff_ms(attempt):
    capped_attempt = min(attempt, RETRY_POLICY_MAX_ATTEMPTS)
    return 100 * (2 ** capped_attempt)


def reconnect_with_backoff(conn, attempt):
    delay_ms = compute_backoff_ms(attempt)
    conn.wait(delay_ms)
    conn.reconnect()


def connect_on_startup(conn):
    reconnect_with_backoff(conn, 0)
