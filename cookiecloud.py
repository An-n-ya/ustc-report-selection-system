#!/usr/bin/env python3
"""Pull the portal session cookies from a self hosted CookieCloud instance.

CookieCloud keeps an encrypted copy of the browser cookies, so this tool can
take its session from there instead of asking for a manual paste. The sync
credentials live in the environment rather than in the repository:

    COOKIECLOUD_HOST      base url of the server, e.g. https://cookie.annya.work
    COOKIECLOUD_UUID      the sync uuid chosen in the browser extension
    COOKIECLOUD_PASSWORD  the end to end encryption password

The payload uses the scheme documented by CookieCloud: the aes key is the first
16 hex characters of md5(uuid-password), and the ciphertext is either the
CryptoJS envelope (a "Salted__" header followed by the salt, with key and iv
derived through EVP_BytesToKey) or plain aes-128-cbc under an all zero iv.
Decryption is delegated to the openssl command line tool so that the project
keeps its promise of having no third party Python dependencies.

Run this module directly to inspect what CookieCloud would hand over:

    python3 cookiecloud.py yjs1.ustc.edu.cn
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

HOST_ENV = "COOKIECLOUD_HOST"
UUID_ENV = "COOKIECLOUD_UUID"
PASSWORD_ENV = "COOKIECLOUD_PASSWORD"

FETCH_TIMEOUT = 20
DECRYPT_TIMEOUT = 30

SALTED_HEADER = b"Salted__"
SALT_SIZE = 8
DERIVED_SIZE = 48
FIXED_IV = b"\x00" * 16

NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
NETSCAPE_HTTPONLY_PREFIX = "#HttpOnly_"


class CookieCloudError(Exception):
    """Raised when the snapshot cannot be fetched, decrypted or converted."""


# --------------------------------------------------------------------- config


def load_config() -> dict:
    """Read the CookieCloud settings from the environment.

    Returns an empty mapping unless all three variables are set, so a partly
    configured environment simply leaves the feature switched off.
    """
    endpoint = (os.environ.get(HOST_ENV) or "").strip().rstrip("/")
    uuid = (os.environ.get(UUID_ENV) or "").strip()
    password = os.environ.get(PASSWORD_ENV) or ""
    if not endpoint or not uuid or not password:
        return {}
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    return {"endpoint": endpoint, "uuid": uuid, "password": password}


# ------------------------------------------------------------------- decrypt


def _derive_key(uuid: str, password: str) -> bytes:
    """The aes key is the first 16 hex characters of md5(uuid-password)."""
    digest = hashlib.md5(("%s-%s" % (uuid, password)).encode("utf-8")).hexdigest()
    return digest[:16].encode("ascii")


def _evp_bytes_to_key(password: bytes, salt: bytes, size: int) -> bytes:
    """OpenSSL's legacy key derivation, which CryptoJS uses for its envelope."""
    derived = b""
    block = b""
    while len(derived) < size:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:size]


def _openssl_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Run one aes-cbc decryption through the openssl command line tool."""
    command = [
        "openssl", "enc", "-d", "-aes-%d-cbc" % (len(key) * 8),
        "-K", key.hex(), "-iv", iv.hex(),
    ]
    try:
        result = subprocess.run(
            command,
            input=ciphertext,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DECRYPT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise CookieCloudError(
            "the openssl command line tool is needed to decrypt CookieCloud data"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CookieCloudError("decrypting the CookieCloud payload timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or "unknown error"
        raise CookieCloudError("openssl rejected the payload: %s" % detail[:200])
    return result.stdout


def decrypt(uuid: str, password: str, encrypted: str) -> dict:
    """Turn one encrypted blob into the decrypted CookieCloud document."""
    try:
        raw = base64.b64decode(encrypted)
    except (binascii.Error, ValueError) as exc:
        raise CookieCloudError("the CookieCloud payload is not valid base64") from exc

    key = _derive_key(uuid, password)
    if raw.startswith(SALTED_HEADER):
        # CryptoJS envelope: header, then the salt, then the ciphertext.
        salt = raw[len(SALTED_HEADER):len(SALTED_HEADER) + SALT_SIZE]
        ciphertext = raw[len(SALTED_HEADER) + SALT_SIZE:]
        material = _evp_bytes_to_key(key, salt, DERIVED_SIZE)
        plaintext = _openssl_decrypt(material[:32], material[32:48], ciphertext)
    else:
        plaintext = _openssl_decrypt(key, FIXED_IV, raw)

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CookieCloudError(
            "the payload did not decrypt into JSON; check the uuid and password"
        ) from exc


# --------------------------------------------------------------------- fetch


def fetch(endpoint: str, uuid: str, timeout: int = FETCH_TIMEOUT) -> dict:
    """Read the raw /get response for one uuid."""
    url = "%s/get/%s" % (endpoint.rstrip("/"), urllib.parse.quote(uuid, safe=""))
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CookieCloudError(
                "CookieCloud has no snapshot for uuid %s" % uuid
            ) from exc
        raise CookieCloudError("CookieCloud answered with HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise CookieCloudError("cannot reach CookieCloud: %s" % exc.reason) from exc
    except OSError as exc:
        raise CookieCloudError("cannot reach CookieCloud: %s" % exc) from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise CookieCloudError("CookieCloud answered with a non JSON document") from exc
    if not isinstance(payload, dict) or not payload.get("encrypted"):
        raise CookieCloudError(
            "CookieCloud returned no encrypted snapshot for uuid %s" % uuid
        )
    return payload


# ------------------------------------------------------------------ netscape


def _applies_to(domain: str, host: str) -> bool:
    """True when a cookie scoped to domain would be sent to host."""
    domain = (domain or "").lstrip(".").lower()
    host = (host or "").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def netscape(cookie_data: dict, host: str) -> str:
    """Serialise the cookies that apply to host as a Netscape cookies.txt.

    CookieCloud stores unrelated sites side by side with the portal, so the
    snapshot is filtered down to the cookies a browser would actually send to
    host. Everything else stays out of data/session.json.
    """
    lines = [NETSCAPE_HEADER]
    for fallback_domain, items in (cookie_data or {}).items():
        for item in items or []:
            name = item.get("name")
            if not name:
                continue
            domain = item.get("domain") or fallback_domain or ""
            if not _applies_to(domain, host):
                continue
            expires = item.get("expirationDate")
            try:
                expires = int(expires) if expires else 0
            except (TypeError, ValueError):
                expires = 0
            line = "\t".join(
                (
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    item.get("path") or "/",
                    "TRUE" if item.get("secure") else "FALSE",
                    str(expires),
                    name,
                    item.get("value") or "",
                )
            )
            if item.get("httpOnly"):
                line = NETSCAPE_HTTPONLY_PREFIX + line
            lines.append(line)

    if len(lines) == 1:
        raise CookieCloudError(
            "CookieCloud holds no cookies for %s; sync the portal in the browser first"
            % host
        )
    return "\n".join(lines)


class CookieCloudClient:
    """Fetches, decrypts and filters the snapshot for one uuid."""

    def __init__(
        self, endpoint: str, uuid: str, password: str, timeout: int = FETCH_TIMEOUT
    ) -> None:
        self.endpoint = endpoint
        self.uuid = uuid
        self.password = password
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        """Build a client from the environment, or None when unconfigured."""
        config = load_config()
        if not config:
            return None
        return cls(**config)

    def snapshot(self) -> dict:
        payload = fetch(self.endpoint, self.uuid, self.timeout)
        return decrypt(self.uuid, self.password, payload["encrypted"])

    def netscape(self, host: str) -> str:
        document = self.snapshot()
        return netscape(document.get("cookie_data") or {}, host)

    def describe(self) -> dict:
        """Status details that are safe to expose to the browser."""
        return {"configured": True, "endpoint": self.endpoint}


def main() -> None:
    parser = argparse.ArgumentParser(description="CookieCloud snapshot helper")
    parser.add_argument("host", nargs="?", default="yjs1.ustc.edu.cn",
                        help="host whose cookies should be listed")
    args = parser.parse_args()

    client = CookieCloudClient.from_env()
    if not client:
        print("Set %s, %s and %s first." % (HOST_ENV, UUID_ENV, PASSWORD_ENV))
        raise SystemExit(1)

    document = client.snapshot()
    names = sorted(
        "%s/%s" % (domain, item.get("name"))
        for domain, items in (document.get("cookie_data") or {}).items()
        for item in items or []
    )
    print("snapshot holds %d cookies across %d domains"
          % (len(names), len(document.get("cookie_data") or {})))
    print("cookies for %s:" % args.host)
    print(client.netscape(args.host))


if __name__ == "__main__":
    main()
