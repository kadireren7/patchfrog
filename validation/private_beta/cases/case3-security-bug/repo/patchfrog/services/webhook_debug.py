"""Debug helper for troubleshooting slow third-party notification
deliveries (private beta validation sprint fixture -- not part of the
real product)."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def log_outgoing_notification(*, url: str, api_key: str, payload_size_bytes: int) -> None:
    """Logs a diagnostic line for every outgoing third-party notification
    call, so slow/failing deliveries can be correlated by URL."""

    logger.info(
        "outgoing_notification_dispatched",
        url=url,
        api_key=api_key,
        payload_size_bytes=payload_size_bytes,
    )
