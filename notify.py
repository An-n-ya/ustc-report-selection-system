#!/usr/bin/env python3
"""ServerChan push notifications for session expiry and enrolment alerts.

The tool only touches the school portal when the browser or the auto enrol
worker asks it to, so a stale cookie can go unnoticed for days. This module
sends one push notification the first time the stored cookie stops being
accepted, and then stays quiet until the session is healthy again.

Enrolment alerts work the other way round: every report that is actually taken
is worth a message, so one push is sent per batch. The auto enrol worker hands
over a whole pass at once, which keeps a burst of sign ups from burning through
the daily ServerChan quota.

Failed sign ups are reported too, otherwise a report that quietly refuses the
student leaves no trace outside the local log. Those messages are batched per
attempt and de-duplicated by the set of failures, because the worker retries the
same reports on every pass: an unchanged refusal only pushes once, and a later
successful sign up clears the memory so a fresh failure alerts again.

Credentials and de-duplication state share a single JSON file under data/, so
restarting the process does not turn one expiry into a stream of repeats.
User facing copy lives in i18n/zh.json rather than in this file.

Run this module directly to send a test push:

    python3 notify.py
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SEND_ENDPOINT = "https://sctapi.ftqq.com/%s.send"
SEND_TIMEOUT = 15

DEFAULT_CHECK_INTERVAL_MINUTES = 30
MIN_CHECK_INTERVAL_MINUTES = 5
MAX_CHECK_INTERVAL_MINUTES = 1440

DEFAULT_SITE_URL = "https://report.annya.work/"
SENDKEY_ENV = "SERVERCHAN_SENDKEY"

ROOT = os.path.dirname(os.path.abspath(__file__))
I18N_FILE = os.path.join(ROOT, "i18n", "zh.json")
DEFAULT_PATH = os.path.join(ROOT, "data", "notify.json")


def _texts() -> dict:
    try:
        with open(I18N_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _stamp(when=None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when or time.time()))


class ServerChanNotifier:
    """Owns the ServerChan send key and the once-per-expiry alert state."""

    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.sendkey = ""
        self.enabled = True
        self.check_interval_minutes = DEFAULT_CHECK_INTERVAL_MINUTES
        self.site_url = DEFAULT_SITE_URL
        self.expiry_alerted = False
        self.last_alert_at = 0.0
        self.last_result = ""
        self.last_error = ""
        self.last_enroll_at = 0.0
        self.last_enroll_result = ""
        self.last_enroll_fail_at = 0.0
        self.last_enroll_fail_result = ""
        self._enroll_fail_signature = ""
        self._sending = False
        self.load()

    # ------------------------------------------------------------ persistence

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            payload = {}
        self.sendkey = (payload.get("sendkey") or "").strip()
        self.enabled = bool(payload.get("enabled", True))
        self.check_interval_minutes = self._clamp(
            payload.get("checkIntervalMinutes") or DEFAULT_CHECK_INTERVAL_MINUTES
        )
        self.site_url = (payload.get("siteUrl") or DEFAULT_SITE_URL).strip()
        self.expiry_alerted = bool(payload.get("expiryAlerted", False))
        self.last_alert_at = float(payload.get("lastAlertAt") or 0)
        self.last_result = payload.get("lastResult") or ""
        self.last_error = payload.get("lastError") or ""
        self.last_enroll_at = float(payload.get("lastEnrollAt") or 0)
        self.last_enroll_result = payload.get("lastEnrollResult") or ""
        self.last_enroll_fail_at = float(payload.get("lastEnrollFailAt") or 0)
        self.last_enroll_fail_result = payload.get("lastEnrollFailResult") or ""
        self._enroll_fail_signature = payload.get("enrollFailSignature") or ""
        override = (os.environ.get(SENDKEY_ENV) or "").strip()
        if override:
            self.sendkey = override

    def save(self) -> None:
        payload = {
            "sendkey": self.sendkey,
            "enabled": self.enabled,
            "checkIntervalMinutes": self.check_interval_minutes,
            "siteUrl": self.site_url,
            "expiryAlerted": self.expiry_alerted,
            "lastAlertAt": self.last_alert_at,
            "lastResult": self.last_result,
            "lastError": self.last_error,
            "lastEnrollAt": self.last_enroll_at,
            "lastEnrollResult": self.last_enroll_result,
            "lastEnrollFailAt": self.last_enroll_fail_at,
            "lastEnrollFailResult": self.last_enroll_fail_result,
            "enrollFailSignature": self._enroll_fail_signature,
        }
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _clamp(value) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return DEFAULT_CHECK_INTERVAL_MINUTES
        return max(MIN_CHECK_INTERVAL_MINUTES, min(MAX_CHECK_INTERVAL_MINUTES, minutes))

    # ------------------------------------------------------------------ send

    def send(self, title: str, body: str) -> tuple:
        """Post a single push. Returns (ok, detail) and never raises."""
        with self.lock:
            sendkey = self.sendkey
            enabled = self.enabled
        if not enabled:
            return False, "notifications are disabled"
        if not sendkey:
            return False, "no send key configured"
        payload = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
        request = urllib.request.Request(
            SEND_ENDPOINT % urllib.parse.quote(sendkey, safe=""),
            data=payload,
            method="POST",
        )
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return False, "cannot reach ServerChan: %s" % exc.reason
        except OSError as exc:
            return False, "cannot reach ServerChan: %s" % exc
        try:
            data = json.loads(raw)
        except ValueError:
            return False, "unexpected ServerChan response: %s" % raw[:200]
        try:
            # ServerChan answers with code 0 on success, so the value cannot be
            # tested for truthiness.
            code = int(data.get("code"))
        except (TypeError, ValueError):
            return False, "unexpected ServerChan response: %s" % raw[:200]
        if code != 0:
            message = data.get("message") or raw[:200]
            return False, "ServerChan rejected the push: %s" % message
        return True, "sent"

    # ---------------------------------------------------------------- alerts

    def alert_expired(self, reason: str) -> bool:
        """Queue the expiry alert, at most once per expired session.

        The push runs on a helper thread so that a slow or unreachable
        ServerChan never delays the request that discovered the expiry.
        """
        with self.lock:
            if self.expiry_alerted or self._sending:
                return False
            self._sending = True
        threading.Thread(
            target=self._alert_worker, args=(reason,), name="notify-expiry", daemon=True
        ).start()
        return True

    def _alert_worker(self, reason: str) -> None:
        texts = _texts()
        title = texts.get("notify.expiredTitle") or "Session cookie expired"
        template = texts.get("notify.expiredBody") or (
            "The stored session cookie is no longer accepted.\n\n"
            "Detected: {time}\n\nReason: {reason}\n\nOpen {url} to reconnect."
        )
        body = template.format(
            time=_stamp(), reason=reason or "-", url=self.site_url
        )
        ok, detail = self.send(title, body)
        with self.lock:
            self._sending = False
            if ok:
                self.expiry_alerted = True
                self.last_alert_at = time.time()
                self.last_result = detail
                self.last_error = ""
            else:
                # Leave the alert flag clear so the next health check retries.
                self.last_error = detail
            self.save()

    def mark_healthy(self) -> None:
        """Forget the previous expiry so a future one alerts again."""
        with self.lock:
            if not self.expiry_alerted and not self.last_error:
                return
            self.expiry_alerted = False
            self.last_error = ""
            self.save()

    # ------------------------------------------------------------- enrolments

    def alert_enrolled(self, titles) -> bool:
        """Queue one push naming the reports that were just taken.

        Unlike the expiry alert this is never de-duplicated: every batch of
        successful sign ups deserves a message. Callers pass a whole pass at
        once so a burst of enrolments costs a single ServerChan message.
        """
        titles = [str(title).strip() for title in titles if str(title).strip()]
        if not titles:
            return False
        with self.lock:
            if not self.enabled or not self.sendkey:
                return False
        threading.Thread(
            target=self._enrolled_worker,
            args=(titles,),
            name="notify-enrolled",
            daemon=True,
        ).start()
        return True

    def _enrolled_worker(self, titles) -> None:
        texts = _texts()
        title = texts.get("notify.enrollTitle") or "Reports enrolled"
        template = texts.get("notify.enrollBody") or (
            "Enrolled {count} report(s) at {time}.\n\n{items}\n\nOpen {url}."
        )
        body = template.format(
            time=_stamp(),
            count=len(titles),
            items="\n".join("- %s" % item for item in titles),
            url=self.site_url,
        )
        ok, detail = self.send(title, body)
        with self.lock:
            self.last_enroll_at = time.time()
            self.last_enroll_result = detail
            if ok:
                # A sign up went through, so the state the previous failure
                # described no longer holds: let the same refusal alert again.
                self._enroll_fail_signature = ""
            self.save()

    def alert_enroll_failed(self, failures) -> bool:
        """Queue one push naming the reports this attempt could not take.

        ``failures`` is an iterable of ``(title, reason)`` pairs and is sent as
        a single message, so a pass that hits several refusals still costs one
        ServerChan push. The batch is de-duplicated by its exact contents: as
        long as the same reports keep failing with the same reasons, only the
        first push goes out, which keeps the auto enrol worker from repeating
        the message on every interval.
        """
        cleaned = []
        for item in failures:
            try:
                name, reason = item
            except (TypeError, ValueError):
                continue
            name = str(name or "").strip()
            if not name:
                continue
            cleaned.append((name, str(reason or "").strip()))
        if not cleaned:
            return False
        signature = json.dumps(sorted(cleaned), ensure_ascii=False)
        with self.lock:
            if not self.enabled or not self.sendkey:
                return False
            if signature == self._enroll_fail_signature:
                return False
        threading.Thread(
            target=self._enroll_failed_worker,
            args=(cleaned, signature),
            name="notify-enroll-failed",
            daemon=True,
        ).start()
        return True

    def _enroll_failed_worker(self, failures, signature) -> None:
        texts = _texts()
        title = texts.get("notify.enrollFailTitle") or "Enrolling failed"
        template = texts.get("notify.enrollFailBody") or (
            "Failed to enroll {count} report(s) at {time}.\n\n{items}\n\nOpen {url}."
        )
        body = template.format(
            time=_stamp(),
            count=len(failures),
            items="\n".join(
                "- %s -> %s" % (name, reason or "-") for name, reason in failures
            ),
            url=self.site_url,
        )
        ok, detail = self.send(title, body)
        with self.lock:
            self.last_enroll_fail_at = time.time()
            self.last_enroll_fail_result = detail
            if ok:
                self._enroll_fail_signature = signature
            # On failure the memory stays empty, so the next pass retries.
            self.save()

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "configured": bool(self.sendkey),
                "checkIntervalMinutes": self.check_interval_minutes,
                "siteUrl": self.site_url,
                "expiryAlerted": self.expiry_alerted,
                "lastAlertAt": self.last_alert_at,
                "lastResult": self.last_result,
                "lastError": self.last_error,
                "lastEnrollAt": self.last_enroll_at,
                "lastEnrollResult": self.last_enroll_result,
                "lastEnrollFailAt": self.last_enroll_fail_at,
                "lastEnrollFailResult": self.last_enroll_fail_result,
            }


def main() -> None:
    parser = argparse.ArgumentParser(description="ServerChan notification helper")
    parser.add_argument("--path", default=DEFAULT_PATH, help="path to notify.json")
    parser.add_argument("--message", default="", help="override the test message body")
    args = parser.parse_args()

    notifier = ServerChanNotifier(args.path)
    texts = _texts()
    title = texts.get("notify.testTitle") or "ServerChan connectivity test"
    body = args.message or texts.get("notify.testBody") or "Test push from the report manager."
    ok, detail = notifier.send(title, body)
    print("%s: %s" % ("ok" if ok else "failed", detail))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
