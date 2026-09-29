#!/usr/bin/env python3
"""ServerChan push notifications for session expiry alerts.

The tool only touches the school portal when the browser or the auto enrol
worker asks it to, so a stale cookie can go unnoticed for days. This module
sends one push notification the first time the stored cookie stops being
accepted, and then stays quiet until the session is healthy again.

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
