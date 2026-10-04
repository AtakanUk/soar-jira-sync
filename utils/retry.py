from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import TypeVar

from config import get_settings
from utils.logger import get_logger

F = TypeVar("F", bound=Callable[..., object])
log = get_logger("retry")


def with_retry(
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    max_attempts: int | None = None,
    backoff_base: int | None = None,
    backoff_max: int | None = None,
) -> Callable[[F], F]:
    settings = get_settings().retry
    attempts = max_attempts or settings.max_attempts
    base = backoff_base or settings.backoff_base
    cap = backoff_max or settings.backoff_max

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # type: ignore[misc]
                    last_exc = exc
                    if attempt == attempts:
                        log.error("Call failed after %s attempts: %s", attempts, exc)
                        raise
                    wait_seconds = min(base * (2 ** (attempt - 1)), cap)
                    log.warning(
                        "Attempt %s/%s failed for %s: %s. Retrying in %ss.",
                        attempt,
                        attempts,
                        func.__name__,
                        exc,
                        wait_seconds,
                    )
                    time.sleep(wait_seconds)
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("Retry wrapper exited unexpectedly")

        return wrapper  # type: ignore[return-value]

    return decorator
