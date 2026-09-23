"""Hourly worker that enrolls in open academic reports on the student's behalf.

A pass only considers reports that the portal already marks as selectable and
that still have seats left, and it stops once the account holds the configured
number of reports. Every pass appends to a rolling log that the web UI shows,
so the student can always see what was taken and what was refused.
"""

from __future__ import annotations

import json
import os
import threading
import time

from ustc_api import COMPUTER_DEPT_CODES, SessionExpiredError, UstcApiError

DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_TARGET = 10
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440
LOG_LIMIT = 200
LOG_TAIL = 40
POLL_SECONDS = 5
ENROLL_DELAY_SECONDS = 1.5
COMPUTER_DEPT_SET = frozenset(COMPUTER_DEPT_CODES)


def has_seats(row: dict) -> bool:
    """True when the report still has room, or when capacity is unknown."""
    try:
        return int(row.get("YXRS")) < int(row.get("KXRS"))
    except (TypeError, ValueError):
        # No usable capacity numbers: defer to the portal's selectable flag.
        return True


def is_open(row: dict) -> bool:
    return str(row.get("SFKXK")) == "1"


class AutoEnroller:
    """Owns the schedule, the enrolment policy and the visible log."""

    def __init__(self, store, path: str) -> None:
        self.store = store
        self.path = path
        self.lock = threading.RLock()
        self.enabled = False
        self.interval_minutes = DEFAULT_INTERVAL_MINUTES
        self.target = DEFAULT_TARGET
        self.entries = []
        self.last_run = 0.0
        self.next_run = 0.0
        self.last_summary = ""
        self.running = False
        self._thread = None
        self._stop = threading.Event()
        self.load()

    # ------------------------------------------------------------- persistence

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        self.interval_minutes = self._clamp_interval(
            payload.get("intervalMinutes") or DEFAULT_INTERVAL_MINUTES
        )
        self.target = max(1, int(payload.get("target") or DEFAULT_TARGET))
        self.entries = payload.get("entries") or []
        self.last_run = float(payload.get("lastRun") or 0)
        self.last_summary = payload.get("lastSummary") or ""
        # A worker that was armed when the process died must not silently resume
        # writing to the school system; the student re-arms it explicitly.
        self.enabled = False
        self.next_run = 0.0

    def save(self) -> None:
        payload = {
            "enabled": self.enabled,
            "intervalMinutes": self.interval_minutes,
            "target": self.target,
            "lastRun": self.last_run,
            "lastSummary": self.last_summary,
            "entries": self.entries[-LOG_LIMIT:],
        }
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @staticmethod
    def _clamp_interval(value) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_MINUTES
        return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, minutes))

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="auto-enroller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            with self.lock:
                due = self.enabled and self.next_run and time.time() >= self.next_run
            if not due:
                continue
            try:
                self.run_once(trigger="schedule")
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self._record("error", "pass crashed: %s" % exc)
                with self.lock:
                    self.next_run = time.time() + self.interval_minutes * 60
                    self.save()

    # ------------------------------------------------------------------- state

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "intervalMinutes": self.interval_minutes,
                "target": self.target,
                "running": self.running,
                "lastRun": self.last_run,
                "nextRun": self.next_run,
                "lastSummary": self.last_summary,
                "entries": self.entries[-LOG_TAIL:],
            }

    def configure(self, payload: dict) -> dict:
        with self.lock:
            was_enabled = self.enabled
            interval_changed = False
            if "target" in payload:
                self.target = max(1, int(payload["target"]))
            if "intervalMinutes" in payload:
                new_interval = self._clamp_interval(payload["intervalMinutes"])
                interval_changed = new_interval != self.interval_minutes
                self.interval_minutes = new_interval
            if "enabled" in payload:
                self.enabled = bool(payload["enabled"])

            if self.enabled and not was_enabled:
                # Give the student one full interval to change their mind before
                # anything is written to the school system.
                self.next_run = time.time() + self.interval_minutes * 60
                self._record(
                    "info",
                    "auto enroll armed, first pass in %d min" % self.interval_minutes,
                )
            elif self.enabled and interval_changed:
                self.next_run = time.time() + self.interval_minutes * 60
                self._record("info", "interval now %d min" % self.interval_minutes)
            elif not self.enabled and was_enabled:
                self.next_run = 0.0
                self._record("info", "auto enroll paused")
            self.save()
            return self.status()

    def clear_log(self) -> dict:
        with self.lock:
            self.entries = []
            self.last_summary = ""
            self.save()
            return self.status()

    # -------------------------------------------------------------------- pass

    def run_once(self, trigger: str = "manual") -> dict:
        with self.lock:
            if self.running:
                return {"enrolled": 0, "reason": "a pass is already running"}
            self.running = True
        try:
            result = self._pass(trigger)
        finally:
            with self.lock:
                self.running = False
                self.last_run = time.time()
                self.next_run = (
                    self.last_run + self.interval_minutes * 60 if self.enabled else 0.0
                )
                self.save()
        return result

    def _pass(self, trigger: str) -> dict:
        store = self.store
        if not store.client:
            self._record("skip", "no active session, nothing to do")
            return {"enrolled": 0, "reason": "no session"}

        try:
            store.refresh()
        except SessionExpiredError as exc:
            self._pause("session expired: %s" % exc)
            return {"enrolled": 0, "reason": "session expired"}
        except UstcApiError as exc:
            self._record("error", "could not read the report list: %s" % exc)
            return {"enrolled": 0, "reason": str(exc)}

        held = store.selected
        already = {item["bgbm"] for item in held}
        quota = self.target - len(held)
        if quota <= 0:
            self._record(
                "idle",
                "already holding %d reports, target is %d" % (len(held), self.target),
            )
            self._set_summary("holding %d/%d, nothing to do" % (len(held), self.target))
            return {"enrolled": 0, "reason": "target reached"}

        candidates = [
            item
            for item in store.available
            if item["deptCode"] in COMPUTER_DEPT_SET
            and item["bgbm"] not in already
            and item["canEnroll"]
            and has_seats(item["raw"])
        ]
        # Grab the reports whose sign up window closes first.
        candidates.sort(key=lambda item: item["deadline"] or "9999")

        if not candidates:
            self._record(
                "idle",
                "no selectable report with free seats right now (holding %d/%d)"
                % (len(held), self.target),
            )
            self._set_summary("holding %d/%d, no candidate" % (len(held), self.target))
            return {"enrolled": 0, "reason": "no candidate"}

        enrolled = []
        refused = []
        for item in candidates[:quota]:
            try:
                ok, message = store.enroll(item["bgbm"])
            except SessionExpiredError as exc:
                self._pause("session expired mid pass: %s" % exc)
                break
            except UstcApiError as exc:
                refused.append(item)
                self._record("refused", "%s -> %s" % (item["title"], exc))
                continue
            if ok:
                enrolled.append(item)
                self._record("enrolled", "%s [%s]" % (item["title"], item["bgbm"]))
                time.sleep(ENROLL_DELAY_SECONDS)
            else:
                refused.append(item)
                self._record("refused", "%s -> %s" % (item["title"], message or "rejected"))

        if enrolled:
            try:
                store.refresh()
            except UstcApiError as exc:
                self._record("error", "refresh after enrolling failed: %s" % exc)

        total = len(held) + len(enrolled)
        summary = "found %d candidate(s), enrolled %d, refused %d, holding %d/%d" % (
            len(candidates),
            len(enrolled),
            len(refused),
            total,
            self.target,
        )
        self._record("summary", summary)
        self._set_summary(summary)
        return {
            "enrolled": len(enrolled),
            "refused": len(refused),
            "candidates": len(candidates),
            "holding": total,
            "target": self.target,
            "trigger": trigger,
        }

    # ------------------------------------------------------------------ logging

    def _pause(self, message: str) -> None:
        with self.lock:
            self.enabled = False
            self.next_run = 0.0
            self._record("error", "%s -- auto enroll paused" % message)

    def _set_summary(self, text: str) -> None:
        with self.lock:
            self.last_summary = text

    def _record(self, kind: str, message: str) -> None:
        with self.lock:
            self.entries.append({"at": time.time(), "kind": kind, "message": message})
            if len(self.entries) > LOG_LIMIT:
                del self.entries[:-LOG_LIMIT]