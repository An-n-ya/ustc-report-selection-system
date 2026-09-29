#!/usr/bin/env python3
"""Local web server for managing USTC graduate academic reports.

Only reports published by computer related departments are exposed. The server
keeps a session cookie, caches the upstream report list on disk and offers a
small JSON API consumed by the bundled single page front end.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from auto_enroll import AutoEnroller
from notify import ServerChanNotifier
from session_watch import SessionWatcher
from ustc_api import (
    ACTION_AVAILABLE,
    COMPUTER_DEPT_CODES,
    SessionExpiredError,
    UstcApiError,
    UstcReportClient,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
DATA_DIR = os.path.join(ROOT, "data")
I18N_DIR = os.path.join(ROOT, "i18n")
SESSION_FILE = os.path.join(DATA_DIR, "session.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")
AUTO_FILE = os.path.join(DATA_DIR, "auto_enroll.json")
NOTIFY_FILE = os.path.join(DATA_DIR, "notify.json")

CACHE_TTL_SECONDS = 30 * 60
COMPUTER_DEPT_SET = frozenset(COMPUTER_DEPT_CODES)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def load_departments() -> dict:
    path = os.path.join(DATA_DIR, "departments.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {row["code"]: row for row in rows if row.get("code")}


def normalize_report(row: dict, dept_names: dict) -> dict:
    code = row.get("YXDM") or ""
    known = dept_names.get(code) or {}
    capacity = row.get("KXRS")
    enrolled = row.get("YXRS")
    return {
        "bgbm": row.get("BGBM"),
        "title": row.get("BGTMZW") or "",
        "titleEn": row.get("BGTMYW") or "",
        "speaker": row.get("BGRZW") or "",
        "deptCode": code,
        "deptName": row.get("YXDM_DISPLAY") or known.get("name") or code,
        "location": row.get("DD") or "",
        "time": row.get("BGSJ") or "",
        "deadline": row.get("JZSJ") or "",
        "capacity": capacity,
        "enrolled": enrolled,
        "canEnroll": str(row.get("SFKXK")) == "1",
        "canDrop": str(row.get("SFKTK")) == "1",
        "remark": row.get("BZ") or "",
        "raw": row,
    }


def report_status(item: dict, scope: str) -> str:
    if scope == "selected":
        return "droppable" if item["canDrop"] else "locked"
    if item["canEnroll"]:
        return "open"
    try:
        if item["capacity"] is not None and item["enrolled"] is not None:
            if int(item["enrolled"]) >= int(item["capacity"]):
                return "full"
    except (TypeError, ValueError):
        pass
    return "closed"


class Store:
    """Holds the upstream session plus an in-memory copy of the report lists."""

    def __init__(self, notifier=None) -> None:
        self.lock = threading.RLock()
        self.client = None
        self.notifier = notifier
        self.departments = load_departments()
        self.available = []
        self.selected = []
        self.credits = None
        self.fetched_at = 0.0
        self.cookie_hint = ""
        self.expired_at = 0.0
        self.expired_reason = ""
        self.load_session()
        self.load_cache()

    # ------------------------------------------------------------- persistence

    def load_session(self) -> None:
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        cookies = payload.get("cookies") or ""
        if not cookies:
            return
        try:
            self.client = UstcReportClient(cookies)
            self.cookie_hint = ", ".join(self.client.cookie_names())
        except UstcApiError:
            self.client = None

    def save_session(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {"cookies": self.client.export_cookies() if self.client else ""}
        with open(SESSION_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass

    def load_cache(self) -> None:
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        self.available = payload.get("available") or []
        self.selected = payload.get("selected") or []
        self.credits = payload.get("credits")
        self.fetched_at = float(payload.get("fetchedAt") or 0)

    def save_cache(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "fetchedAt": self.fetched_at,
            "credits": self.credits,
            "available": self.available,
            "selected": self.selected,
        }
        with open(CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    # ----------------------------------------------------------------- session

    def connect(self, cookies: str) -> dict:
        with self.lock:
            client = UstcReportClient(cookies)
            client.credits()
            self.client = client
            self.cookie_hint = ", ".join(client.cookie_names())
            self.expired_at = 0.0
            self.expired_reason = ""
            try:
                status = self.refresh()
            except Exception:
                self.client = None
                self.cookie_hint = ""
                raise
            self.save_session()
        self.mark_healthy()
        return status

    def disconnect(self) -> None:
        with self.lock:
            self.client = None
            self.cookie_hint = ""
            self.available = []
            self.selected = []
            self.credits = None
            self.fetched_at = 0.0
            self.expired_at = 0.0
            self.expired_reason = ""
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass
            try:
                os.remove(CACHE_FILE)
            except OSError:
                pass
        self.mark_healthy()

    # ------------------------------------------------------- session health

    def probe(self) -> None:
        """Make one cheap authenticated call, raising if the cookie is stale."""
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            self.client.credits()

    def mark_expired(self, reason: str) -> None:
        """Remember that the cookie was rejected and alert the owner once."""
        with self.lock:
            if self.expired_at == 0.0:
                self.expired_at = time.time()
            self.expired_reason = reason
        if self.notifier:
            self.notifier.alert_expired(reason)

    def mark_healthy(self) -> None:
        with self.lock:
            self.expired_at = 0.0
            self.expired_reason = ""
        if self.notifier:
            self.notifier.mark_healthy()

    # ------------------------------------------------------------------- cache

    def refresh(self) -> dict:
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            action = self.client.selected_action()
            available_raw = self.client.fetch_all(ACTION_AVAILABLE)
            selected_raw = self.client.fetch_all(action)
            self.available = [
                item
                for item in (normalize_report(r, self.departments) for r in available_raw)
                if item["deptCode"] in COMPUTER_DEPT_SET
            ]
            self.selected = [
                normalize_report(r, self.departments) for r in selected_raw
            ]
            self.credits = self.client.credits()
            self.fetched_at = time.time()
            self.save_cache()
            return self.status()

    def status(self) -> dict:
        with self.lock:
            return {
                "connected": self.client is not None,
                "cookieFields": self.cookie_hint,
                "fetchedAt": self.fetched_at,
                "stale": (time.time() - self.fetched_at) > CACHE_TTL_SECONDS
                if self.fetched_at
                else True,
                "credits": self.credits,
                "availableCount": len(self.available),
                "selectedCount": len(self.selected),
                "deptCount": len(COMPUTER_DEPT_CODES),
                "sessionExpired": self.expired_at > 0,
                "expiredAt": self.expired_at,
                "expiredReason": self.expired_reason,
            }

    # ------------------------------------------------------------------ queries

    def dept_summary(self) -> list:
        with self.lock:
            counts = {}
            for item in self.available:
                counts[item["deptCode"]] = counts.get(item["deptCode"], 0) + 1
            rows = []
            for code in COMPUTER_DEPT_CODES:
                known = self.departments.get(code) or {}
                rows.append(
                    {
                        "code": code,
                        "name": known.get("name") or code,
                        "nameEn": known.get("en") or "",
                        "available": counts.get(code, 0),
                    }
                )
            return rows

    def query(
        self,
        scope: str,
        keyword: str = "",
        dept_codes=None,
        only_open: bool = False,
        sort: str = "time_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            source = self.selected if scope == "selected" else self.available
            if dept_codes:
                allowed = set(dept_codes)
                if scope != "selected":
                    allowed = COMPUTER_DEPT_SET.intersection(allowed)
            elif scope == "selected":
                # A student's own choices stay visible even when the report was
                # published by a department outside the computer scope.
                allowed = None
            else:
                allowed = COMPUTER_DEPT_SET
            keyword = (keyword or "").strip().lower()
            rows = []
            for item in source:
                if allowed is not None and item["deptCode"] not in allowed:
                    continue
                if only_open and scope != "selected" and not item["canEnroll"]:
                    continue
                if keyword:
                    haystack = " ".join(
                        (
                            item["title"],
                            item["titleEn"],
                            item["speaker"],
                            item["location"],
                            item["deptName"],
                        )
                    ).lower()
                    if keyword not in haystack:
                        continue
                rows.append(item)

            rows.sort(key=self._sort_key(sort), reverse=sort.endswith("_desc"))

            total = len(rows)
            page = max(1, int(page))
            page_size = max(1, min(200, int(page_size)))
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            start = (page - 1) * page_size
            window = rows[start:start + page_size]
            return {
                "scope": scope,
                "total": total,
                "page": page,
                "pageSize": page_size,
                "pages": pages,
                "rows": [dict(item, status=report_status(item, scope)) for item in window],
            }

    @staticmethod
    def _sort_key(sort: str):
        field = {
            "time_desc": "time",
            "time_asc": "time",
            "deadline_asc": "deadline",
            "title_asc": "title",
        }.get(sort, "time")
        if field == "title":
            return lambda item: (item["title"] or "").lower()
        return lambda item: item[field] or ""

    # ----------------------------------------------------------------- mutation

    def enroll(self, bgbm: str) -> tuple:
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            return self.client.enroll(bgbm)

    def drop(self, bgbm: str) -> tuple:
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            return self.client.drop(bgbm)

    def detail(self, bgbm: str) -> dict:
        with self.lock:
            if not self.client:
                raise SessionExpiredError("no active session")
            return self.client.detail(bgbm)


NOTIFIER = ServerChanNotifier(NOTIFY_FILE)
STORE = Store(NOTIFIER)
AUTO = AutoEnroller(STORE, AUTO_FILE)
WATCHER = SessionWatcher(STORE, NOTIFIER)


class Handler(BaseHTTPRequestHandler):
    server_version = "ReportManager/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 - signature fixed by base class
        if self.path.startswith("/api/"):
            return
        return

    # ------------------------------------------------------------------ helpers

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400, expired: bool = False) -> None:
        self._json({"ok": False, "error": message, "expired": expired}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {key: value[0] for key, value in parse_qs(raw).items()}

    def _static(self, relative: str) -> None:
        path = os.path.normpath(os.path.join(STATIC_DIR, relative.lstrip("/")))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._error("not found", 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as handle:
            body = handle.read()
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"))

    # --------------------------------------------------------------------- GET

    def do_GET(self):  # noqa: N802 - name fixed by base class
        parsed = urlparse(self.path)
        route = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                self._static("index.html")
            elif route == "/app.js":
                self._static("app.js")
            elif route == "/style.css":
                self._static("style.css")
            elif route == "/api/i18n":
                self._json({"ok": True, "strings": self._i18n()})
            elif route == "/api/status":
                self._json({"ok": True, "status": STORE.status()})
            elif route == "/api/departments":
                self._json({"ok": True, "departments": STORE.dept_summary()})
            elif route == "/api/auto":
                self._json({"ok": True, "auto": AUTO.status()})
            elif route == "/api/notify":
                self._json(
                    {
                        "ok": True,
                        "notify": NOTIFIER.status(),
                        "watcher": WATCHER.status(),
                    }
                )
            elif route == "/api/reports":
                depts = [d for d in (query.get("depts") or "").split(",") if d]
                result = STORE.query(
                    scope=query.get("scope") or "available",
                    keyword=query.get("keyword") or "",
                    dept_codes=depts,
                    only_open=query.get("onlyOpen") == "1",
                    sort=query.get("sort") or "time_desc",
                    page=int(query.get("page") or 1),
                    page_size=int(query.get("pageSize") or 20),
                )
                self._json({"ok": True, "result": result})
            elif route == "/api/report":
                bgbm = query.get("bgbm") or ""
                if not bgbm:
                    self._error("missing bgbm")
                    return
                self._json({"ok": True, "report": STORE.detail(bgbm)})
            else:
                self._error("not found", 404)
        except SessionExpiredError as exc:
            STORE.mark_expired(str(exc))
            self._error(str(exc), 401, expired=True)
        except UstcApiError as exc:
            self._error(str(exc), 502)
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the UI
            self._error("internal error: %s" % exc, 500)

    # -------------------------------------------------------------------- POST

    def do_POST(self):  # noqa: N802 - name fixed by base class
        route = urlparse(self.path).path
        payload = self._body()
        try:
            if route == "/api/connect":
                cookies = payload.get("cookies") or ""
                if not cookies.strip():
                    self._error("cookie string is required")
                    return
                self._json({"ok": True, "status": STORE.connect(cookies)})
            elif route == "/api/disconnect":
                if AUTO.status()["enabled"]:
                    AUTO.configure({"enabled": False})
                STORE.disconnect()
                self._json({"ok": True, "status": STORE.status()})
            elif route == "/api/refresh":
                self._json({"ok": True, "status": STORE.refresh()})
            elif route == "/api/enroll":
                bgbm = payload.get("bgbm") or ""
                if not bgbm:
                    self._error("missing bgbm")
                    return
                ok, message = STORE.enroll(bgbm)
                if ok:
                    STORE.refresh()
                self._json({"ok": ok, "message": message, "status": STORE.status()})
            elif route == "/api/drop":
                bgbm = payload.get("bgbm") or ""
                if not bgbm:
                    self._error("missing bgbm")
                    return
                ok, message = STORE.drop(bgbm)
                if ok:
                    STORE.refresh()
                self._json({"ok": ok, "message": message, "status": STORE.status()})
            elif route == "/api/auto":
                self._json({"ok": True, "auto": AUTO.configure(payload)})
            elif route == "/api/auto/run":
                result = AUTO.run_once(trigger="manual")
                self._json({
                    "ok": True,
                    "result": result,
                    "auto": AUTO.status(),
                    "status": STORE.status(),
                })
            elif route == "/api/auto/log/clear":
                self._json({"ok": True, "auto": AUTO.clear_log()})
            else:
                self._error("not found", 404)
        except SessionExpiredError as exc:
            STORE.mark_expired(str(exc))
            self._error(str(exc), 401, expired=True)
        except UstcApiError as exc:
            self._error(str(exc), 502)
        except Exception as exc:  # noqa: BLE001 - report unexpected failures to the UI
            self._error("internal error: %s" % exc, 500)

    # ------------------------------------------------------------------- i18n

    @staticmethod
    def _i18n() -> dict:
        try:
            with open(os.path.join(I18N_DIR, "zh.json"), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="USTC academic report manager")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    AUTO.start()
    WATCHER.start()
    print("Academic report manager running at http://%s:%d" % (args.host, args.port))
    notify_state = NOTIFIER.status()
    if notify_state["configured"] and notify_state["enabled"]:
        print(
            "Expiry alerts on, checking every %d min."
            % notify_state["checkIntervalMinutes"]
        )
    else:
        print("Expiry alerts off, configure data/notify.json to enable them.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        AUTO.stop()
        WATCHER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
