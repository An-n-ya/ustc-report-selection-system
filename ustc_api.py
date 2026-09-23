"""HTTP client for the USTC graduate academic report selection system.

The upstream service is an EMAP application. Every endpoint is a form-urlencoded
POST that answers with JSON. Authentication is session based: the caller must
supply the cookies produced by the CAS login flow.

Two cookie shapes are accepted, and the right parser is picked automatically:
the "Cookie:" request header copied from the browser DevTools, and the tab
separated Netscape cookies.txt file written by curl, wget or cookie extensions.

Field names used by the upstream payload are kept verbatim so that responses can
be compared against the portal network tab without any translation step.
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie

BASE_URL = "https://yjs1.ustc.edu.cn/gsapp"
APP_NAME = "xsbgglappustc"
HOST = "yjs1.ustc.edu.cn"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

LIST_PATH = "/sys/{app}/modules/xsbgxk/{action}.do"
DETAIL_PATH = "/sys/{app}/modules/xsbgfb/xsbgfbbddz.do"
ENROLLMENT_PATH = "/sys/{app}/modules/xsbgxk/xsjcxxcx.do"
SELECT_PATH = "/sys/{app}/xsbgxkController/selectBg.do"
CANCEL_PATH = "/sys/{app}/xsbgxkController/cancelBg.do"
CREDIT_PATH = "/sys/{app}/xsbgxkController/getCjNum.do"
DEPARTMENT_PATH = "/code/edfd3c2a-dbc4-481b-97ce-b11832a50b78.do"

ACTION_AVAILABLE = "wxbgbgdz"
ACTION_SELECTED = "yxbgbgdz"
ACTION_SELECTED_COMBINED = "szbyxbgbgdz"

# Combined bachelor/master students expose their enrolled reports through a
# different action. The portal detects them through the enrollment mode field.
ENROLLMENT_MODE_COMBINED = "23"

# Department codes that make up the computer science scope of this tool.
COMPUTER_DEPT_CODES = (
    "006",  # electronic engineering and information science
    "010",  # automation
    "011",  # computer science and technology
    "023",  # electronic science and technology
    "210",  # information science and technology
    "218",  # institute of advanced technology
    "219",  # microelectronics
    "221",  # cyberspace security
    "225",  # software engineering
    "229",  # artificial intelligence and data science
    "999",  # future technology
    "A13",  # software engineering, Hefei
    "A14",  # software engineering, Suzhou
)

PAGE_SIZE_CANDIDATES = (5000, 1000, 500, 200, 100, 50, 20)
MAX_PAGES = 400

# The upstream answers with HTTP 200 even when the session is rejected. Those
# payloads carry an error code instead of the usual "0" success code, so the
# envelope has to be inspected before the body is trusted.
AUTH_ERROR_CODE_PREFIX = "#E2140"
AUTH_ERROR_HINTS = ("multiple of 4", "token", "not login", "unauthorized")

# Netscape cookies.txt layout: domain, includeSubdomains, path, secure, expires,
# name, value -- seven tab separated columns per cookie line.
NETSCAPE_MARKER = "netscape http cookie file"
NETSCAPE_HTTPONLY_PREFIX = "#HttpOnly_"
NETSCAPE_FIELD_COUNT = 7


class UstcApiError(Exception):
    """Raised when the upstream service rejects a request."""


class SessionExpiredError(UstcApiError):
    """Raised when the stored cookies no longer authenticate."""


def _first_text(payload: dict) -> str:
    for key in ("message", "msg", "errorMsg", "error", "info"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_auth_failure(payload: dict) -> bool:
    code = str(payload.get("code") or "")
    if code.startswith(AUTH_ERROR_CODE_PREFIX):
        return True
    message = _first_text(payload).lower()
    return any(hint in message for hint in AUTH_ERROR_HINTS)


def _ensure_success(payload: dict) -> dict:
    """Raise unless the envelope carries the success code."""
    code = str(payload.get("code"))
    if code == "0":
        return payload
    message = _first_text(payload) or code
    if _looks_like_auth_failure(payload):
        raise SessionExpiredError(message)
    raise UstcApiError(message)


# ------------------------------------------------------------------ cookie parsing


def _build_cookie(
    name: str,
    value: str,
    domain: str,
    path: str,
    secure: bool,
    expires=None,
    http_only: bool = False,
) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if http_only else {},
        rfc2109=False,
    )


def _netscape_expiry(value: str):
    """Netscape writes 0 for a session cookie, otherwise a unix timestamp."""
    try:
        stamp = int(value.strip())
    except (TypeError, ValueError):
        return None
    return stamp if stamp > 0 else None


def _parse_cookie_header(raw: str) -> list:
    """Parse a browser "Cookie:" header such as "a=1; b=2"."""
    raw = raw.replace("\n", " ").replace("\r", " ")
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:"):].strip()
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except Exception as exc:  # noqa: BLE001 - surface parser detail to caller
        raise UstcApiError("cookie string could not be parsed: %s" % exc) from exc
    return [
        _build_cookie(name, morsel.value, HOST, "/", True)
        for name, morsel in parsed.items()
    ]


def _netscape_fields(line: str):
    """Return the seven columns of a Netscape cookie line, or None.

    The shape is validated rather than guessed: columns two and four are the
    includeSubdomains and secure booleans, column five is the expiry stamp.
    That signature keeps a "a=1; b=2" Cookie header from being misread as a
    cookie file even when it happens to split into seven whitespace tokens.
    """
    fields = line.split("\t")
    if len(fields) < NETSCAPE_FIELD_COUNT:
        fields = line.split(None, NETSCAPE_FIELD_COUNT - 1)
    if len(fields) < NETSCAPE_FIELD_COUNT:
        return None
    fields = [field.strip() for field in fields[:NETSCAPE_FIELD_COUNT]]
    if fields[1].upper() not in ("TRUE", "FALSE"):
        return None
    if fields[3].upper() not in ("TRUE", "FALSE"):
        return None
    try:
        int(fields[4])
    except ValueError:
        return None
    return fields


def _parse_netscape_cookie_file(raw: str) -> list:
    """Parse a curl / wget cookies.txt file, keeping domain, path and flags.

    Comment lines are skipped, except the "#HttpOnly_" prefix which marks a real
    cookie line whose cookie is flagged HttpOnly. Columns are normally tab
    separated; whitespace separated files are accepted as a fallback.
    """
    cookies = []
    for line in raw.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        http_only = False
        if line.startswith(NETSCAPE_HTTPONLY_PREFIX):
            http_only = True
            line = line[len(NETSCAPE_HTTPONLY_PREFIX):]
        elif line.lstrip().startswith("#"):
            continue
        fields = _netscape_fields(line)
        if not fields:
            continue
        domain, _subdomains, path, secure, expires, name, value = fields
        if not domain or not name:
            continue
        cookies.append(
            _build_cookie(
                name=name,
                value=value,
                domain=domain,
                path=path or "/",
                secure=secure.upper() == "TRUE",
                expires=_netscape_expiry(expires),
                http_only=http_only,
            )
        )
    return cookies


def _looks_like_netscape(raw: str) -> bool:
    lowered = raw.lower()
    if NETSCAPE_MARKER in lowered:
        return True
    for line in raw.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith(NETSCAPE_HTTPONLY_PREFIX)):
            continue
        if line.startswith(NETSCAPE_HTTPONLY_PREFIX):
            line = line[len(NETSCAPE_HTTPONLY_PREFIX):]
        if _netscape_fields(line):
            return True
    return False


class UstcReportClient:
    """Thin wrapper around the academic report endpoints."""

    def __init__(self, cookies: str = "", timeout: int = 30) -> None:
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        if cookies:
            self.set_cookies(cookies)

    # ------------------------------------------------------------------ session

    def set_cookies(self, raw: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            raise UstcApiError("empty cookie string")
        cookies = (
            _parse_netscape_cookie_file(raw)
            if _looks_like_netscape(raw)
            else _parse_cookie_header(raw)
        )
        if not cookies:
            raise UstcApiError("no cookie could be parsed from the supplied string")
        self.jar.clear()
        for cookie in cookies:
            self.jar.set_cookie(cookie)

    def export_cookies(self) -> str:
        """Serialise the jar as Netscape cookies.txt so it round-trips exactly."""
        lines = ["# Netscape HTTP Cookie File"]
        for cookie in self.jar:
            lines.append(
                "\t".join(
                    (
                        cookie.domain,
                        "TRUE" if cookie.domain_initial_dot else "FALSE",
                        cookie.path or "/",
                        "TRUE" if cookie.secure else "FALSE",
                        str(cookie.expires or 0),
                        cookie.name,
                        cookie.value,
                    )
                )
            )
        return "\n".join(lines)

    def cookie_names(self) -> list:
        return sorted(c.name for c in self.jar)

    # ------------------------------------------------------------------ request

    def _request(self, path: str, payload: dict = None) -> dict:
        url = BASE_URL + path
        body = urllib.parse.urlencode(payload or {}, doseq=True).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header(
            "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"
        )
        request.add_header("X-Requested-With", "XMLHttpRequest")
        request.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
        request.add_header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Referer", "%s/sys/%s/*default/index.do" % (BASE_URL, APP_NAME))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 302, 303):
                raise SessionExpiredError(
                    "upstream returned HTTP %s, the session is not valid" % exc.code
                ) from exc
            raise UstcApiError("upstream returned HTTP %s" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise UstcApiError("cannot reach the upstream service: %s" % exc.reason) from exc

        text = raw.decode("utf-8", "replace").strip()
        if not text.startswith("{") and not text.startswith("["):
            raise SessionExpiredError(
                "upstream answered with a non JSON document, the session has probably expired"
            )
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise UstcApiError("upstream answered with malformed JSON") from exc
        if _looks_like_auth_failure(data):
            raise SessionExpiredError(
                _first_text(data) or "the upstream service rejected the session"
            )
        return data

    # ------------------------------------------------------------------- reports

    def list_page(
        self,
        action: str,
        page: int = 1,
        page_size: int = 100,
        order: str = "-BGSJ",
        query_setting=None,
    ) -> dict:
        payload = {
            "*order": order,
            "pageSize": str(page_size),
            "pageNumber": str(page),
        }
        if query_setting:
            payload["querySetting"] = json.dumps(query_setting, ensure_ascii=False)
        data = self._request(LIST_PATH.format(app=APP_NAME, action=action), payload)
        _ensure_success(data)
        return (data.get("datas") or {}).get(action) or {}

    def fetch_all(self, action: str, order: str = "-BGSJ", query_setting=None) -> list:
        """Read every page of a report list, picking the largest usable page size."""
        page_size = 20
        rows = []
        total = 0
        for candidate in PAGE_SIZE_CANDIDATES:
            body = self.list_page(action, page=1, page_size=candidate, order=order,
                                  query_setting=query_setting)
            batch = body.get("rows") or []
            total = int(body.get("totalSize") or 0)
            if batch:
                page_size = max(len(batch), 1)
                rows.extend(batch)
                break
            if total == 0:
                return []
        page = 2
        while len(rows) < total and page <= MAX_PAGES:
            body = self.list_page(action, page=page, page_size=page_size, order=order,
                                  query_setting=query_setting)
            batch = body.get("rows") or []
            if not batch:
                break
            rows.extend(batch)
            page += 1
        return rows

    def detail(self, bgbm: str) -> dict:
        data = self._request(
            DETAIL_PATH.format(app=APP_NAME),
            {"pageSize": "1", "pageNumber": "1", "BGBM": bgbm},
        )
        _ensure_success(data)
        rows = ((data.get("datas") or {}).get("xsbgfbbddz") or {}).get("rows") or []
        if not rows:
            raise UstcApiError("report detail not found")
        return rows[0]

    # ------------------------------------------------------------------ mutation

    def enroll(self, bgbm: str) -> tuple:
        return self._outcome(self._request(SELECT_PATH.format(app=APP_NAME), {"BGBM": bgbm}))

    def drop(self, bgbm: str) -> tuple:
        return self._outcome(self._request(CANCEL_PATH.format(app=APP_NAME), {"BGBM": bgbm}))

    @staticmethod
    def _outcome(data: dict) -> tuple:
        if str(data.get("code")) == "0":
            return True, _first_text(data)
        message = _first_text(data)
        if not message:
            message = json.dumps(data, ensure_ascii=False)[:400]
        return False, message

    # ------------------------------------------------------------------ metadata

    def credits(self):
        data = self._request(CREDIT_PATH.format(app=APP_NAME), {})
        _ensure_success(data)
        return (data.get("data") or {}).get("CJ_NUM")

    def departments(self) -> list:
        data = self._request(DEPARTMENT_PATH, {})
        _ensure_success(data)
        return ((data.get("datas") or {}).get("code") or {}).get("rows") or []

    def enrollment_mode(self):
        data = self._request(
            ENROLLMENT_PATH.format(app=APP_NAME), {"pageSize": "1", "pageNumber": "1"}
        )
        _ensure_success(data)
        rows = ((data.get("datas") or {}).get("xsjcxxcx") or {}).get("rows") or []
        if not rows:
            return None
        return rows[0].get("RXFSDM")

    def selected_action(self) -> str:
        try:
            mode = self.enrollment_mode()
        except UstcApiError:
            mode = None
        if str(mode) == ENROLLMENT_MODE_COMBINED:
            return ACTION_SELECTED_COMBINED
        return ACTION_SELECTED