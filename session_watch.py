#!/usr/bin/env python3
"""Background probe that warns when the stored cookie stops working.

Report lists are fetched on demand, so an expired session can sit unnoticed
while the tool is idle. This worker re-checks the session on a fixed interval
and hands the first rejection to the notifier, which owns the de-duplication.
"""

from __future__ import annotations

import threading
import time

from ustc_api import SessionExpiredError, UstcApiError

POLL_SECONDS = 30


class SessionWatcher:
    """Periodically probes the upstream session and reports an expiry once."""

    def __init__(self, store, notifier, recover=None) -> None:
        self.store = store
        self.notifier = notifier
        self.recover = recover
        self.lock = threading.RLock()
        self.last_check = 0.0
        self.last_error = ""
        self.checks = 0
        self.recoveries = 0
        self._thread = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self._loop, name="session-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            if self._due():
                self.check_now()

    def _due(self) -> bool:
        interval = max(1, self.notifier.check_interval_minutes) * 60
        with self.lock:
            return (time.time() - self.last_check) >= interval

    # ------------------------------------------------------------------- probe

    def check_now(self) -> dict:
        with self.lock:
            self.last_check = time.time()
            self.checks += 1

        store = self.store
        if not store.client:
            # Nothing was ever connected, so there is no cookie to refresh.
            return {"ok": True, "skipped": "no session"}

        try:
            store.probe()
        except SessionExpiredError as exc:
            with self.lock:
                self.last_error = ""
            if self._heal():
                # A fresh cookie came back before the owner had to be told.
                return {"ok": True, "recovered": True}
            store.mark_expired(str(exc))
            return {"ok": False, "expired": True, "reason": str(exc)}
        except UstcApiError as exc:
            # A network hiccup is not proof that the cookie went stale.
            with self.lock:
                self.last_error = str(exc)
            return {"ok": False, "expired": False, "reason": str(exc)}

        store.mark_healthy()
        with self.lock:
            self.last_error = ""
        return {"ok": True}

    def _heal(self) -> bool:
        """Try the recovery callback, counting a successful refresh."""
        if not self.recover:
            return False
        try:
            healed = bool(self.recover())
        except Exception:  # noqa: BLE001 - a failed heal just falls through to the alert
            healed = False
        if healed:
            with self.lock:
                self.recoveries += 1
        return healed

    def status(self) -> dict:
        with self.lock:
            return {
                "lastCheck": self.last_check,
                "checks": self.checks,
                "lastError": self.last_error,
                "intervalMinutes": self.notifier.check_interval_minutes,
                "recoveries": self.recoveries,
            }
