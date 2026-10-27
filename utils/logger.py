"""
Centralized logging configuration for all Lambda handlers.
"""

import logging
import sys

from config import LOG_LEVEL


def get_logger(name: str) -> logging.Logger:
    """Return a logger with structured formatting.

    Args:
        name: Logger name, typically __name__ of the calling module.
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    return logger
