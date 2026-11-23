"""Small helper to retry flaky network calls."""

import logging
import time

log = logging.getLogger(__name__)


def with_retry(func, *, attempts=3, delay=2.0, what="request", retry_on=(Exception,), sleep=time.sleep):
    """Call func(). If it raises, wait and try again (delay doubles each time)."""
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_on as e:
            if attempt == attempts:
                raise
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.0fs", what, attempt, attempts, e, delay)
            sleep(delay)
            delay *= 2
