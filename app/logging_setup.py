"""Logging to the console and to a rotating log file."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_file: Path, level: str = "INFO") -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # 1 MB per file, keep 5 old files
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # These libraries are very chatty at INFO level
    for noisy in ("googleapiclient.discovery_cache", "twilio.http_client", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
