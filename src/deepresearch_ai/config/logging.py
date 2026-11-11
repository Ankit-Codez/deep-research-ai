"""Minimal logging configured explicitly by application entry points."""

import logging


def configure_logging(level: str = "INFO") -> None:
    """Send application logs to stderr; keep SDK request logs quiet."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)
    for name in ("httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)
