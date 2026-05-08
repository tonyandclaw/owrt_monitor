from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from owrt_monitor.config import RetryPolicy
    from owrt_monitor.events import EventLogger

T = TypeVar("T")


class JobCancelled(RuntimeError):
    """Raised when a job has been cancelled mid-flight."""


class CancelToken:
    def __init__(self, marker_path: Path) -> None:
        self.marker_path = marker_path

    @property
    def is_cancelled(self) -> bool:
        return self.marker_path.exists()

    def request(self) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text("requested\n", encoding="utf-8")

    def clear(self) -> None:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise JobCancelled("job was cancelled")

    def watch(
        self,
        on_cancel: Callable[[], None],
        *,
        poll_interval_sec: float = 0.5,
    ) -> CancelWatcher:
        return CancelWatcher(self, on_cancel, poll_interval_sec=poll_interval_sec)


class CancelWatcher:
    def __init__(
        self,
        token: CancelToken,
        on_cancel: Callable[[], None],
        *,
        poll_interval_sec: float,
    ) -> None:
        self._token = token
        self._on_cancel = on_cancel
        self._poll_interval_sec = poll_interval_sec
        self._stop = threading.Event()
        self._fired = False
        self._thread = threading.Thread(target=self._run, name="cancel-watcher", daemon=True)

    @property
    def fired(self) -> bool:
        return self._fired

    def __enter__(self) -> CancelWatcher:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._token.is_cancelled:
                self._fired = True
                try:
                    self._on_cancel()
                except Exception:
                    pass
                return
            self._stop.wait(self._poll_interval_sec)


def cancellable_sleep(seconds: float, cancel_token: CancelToken | None) -> None:
    if seconds <= 0:
        return
    if cancel_token is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        cancel_token.raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def with_retry(
    name: str,
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    cancel_token: CancelToken | None = None,
    logger: EventLogger | None = None,
) -> T:
    last_exc: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        try:
            return fn()
        except JobCancelled:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= policy.attempts:
                raise
            if logger is not None:
                logger.emit(
                    level="WARN",
                    component="workflow",
                    event="step_retry",
                    message=f"{name} attempt {attempt} failed: {exc}",
                    fields={
                        "step": name,
                        "attempt": attempt,
                        "max_attempts": policy.attempts,
                        "error": str(exc),
                    },
                )
            cancellable_sleep(policy.backoff_sec, cancel_token)
    assert last_exc is not None
    raise last_exc
