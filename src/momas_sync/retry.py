import random
import threading
from .client import TransientError


class Cancelled(Exception):
    """Graceful shutdown, without logging private operation arguments."""


class Retrier:
    def __init__(self, settings, stop=None, heartbeat=lambda: None, log=lambda *_args, **_kwargs: None, jitter=random.uniform):
        self.settings = settings
        self.stop = stop or threading.Event()
        self.heartbeat = heartbeat
        self.log = log
        self.jitter = jitter

    def wait(self, seconds):
        # Short waits keep the heartbeat alive and SIGTERM responsive.
        remaining = seconds
        while remaining > 0:
            self.heartbeat()
            step = min(remaining, 5)
            if self.stop.wait(step):
                raise Cancelled()
            remaining -= step

    def call(self, operation):
        for attempt in range(self.settings.retry_attempts):
            if self.stop.is_set():
                raise Cancelled()
            self.heartbeat()
            try:
                return operation()
            except TransientError as exc:
                if attempt + 1 == self.settings.retry_attempts:
                    raise
                cap = min(self.settings.retry_cap, self.settings.retry_base * 2 ** attempt)
                delay = self.jitter(cap / 2, cap)
                if exc.retry_after is not None:
                    # Retry-After is a server minimum, not exponential backoff.
                    # Long waits still update heartbeat and respond to SIGTERM.
                    delay = max(delay, exc.retry_after)
                self.log("request_retry", attempt=attempt + 1)
                self.wait(delay)
