"""Lumen proxy service.

Serves the managed exit-proxy repository and the small HTTP API that the
dashboard add-on calls. Runs as its own loopback-only process next to the
panel, so a slow proxy scan can never block panel request handling.

Design notes:

* The repository is private and read with AWS Signature V4. Credentials come
  from the environment only; they are never returned to the browser.
* Endpoint URLs and credentials are never sent to the browser. The UI sees a
  country flag, a health percentage, and a stable ID.
* Proxies are re-tested on a slow schedule by default. A failing repository
  fetch keeps the last good list rather than emptying the catalog.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import countries

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("lumen.proxy")

DATA = Path(os.getenv("LUMEN_DATA", "/var/lib/lumen"))
STORE = DATA / "proxies.json"
API_PORT = int(os.getenv("LUMEN_PROXY_API_PORT", 8100))

BASE_URL = os.getenv("LUMEN_PROXY_REPO_URL", "").strip()
ACCESS_KEY = os.getenv("LUMEN_S3_ACCESS_KEY_ID", "").strip()
SECRET_KEY = os.getenv("LUMEN_S3_SECRET_ACCESS_KEY", "").strip()
REGION = os.getenv("LUMEN_PROXY_REPO_REGION", "us-east-1").strip()
REFRESH_SECONDS = int(os.getenv("LUMEN_PROXY_REFRESH_SECONDS", str(2 * 60 * 60)))
MANUAL_REFRESH_KEY = os.getenv("PROXY_REPOSITORY_MANUAL_REFRESH_KEY", "").strip()

# An idrivee2/S3-compatible endpoint is often written as an origin
# ("https://s3.<region>.idrivee2.com") with the bucket in the path. Both that
# layout and a fully-qualified URL work; this only documents the shape.
S3_ENDPOINT = os.getenv("LUMEN_S3_ENDPOINT", "").strip()

HANDSHAKE_TIMEOUT = 6.0
PROBE_TARGETS = ("https://cloudflare.com", "https://google.com")
CACHE_MAX = 20000

_lock = asyncio.Lock()
_catalog: list[dict[str, Any]] = []
_results: dict[str, dict[str, Any]] = {}
_preferred: dict[str, str] = {}
_assignments: dict[str, str] = {}   # config username -> country code
_last_fetch = 0.0
_last_error = ""
# True once a signed request was refused and an unsigned one succeeded: the
# bucket is public and the keys are not needed.
_unsigned_works = False


# ── Repository ───────────────────────────────────────────────────────────────


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signed_headers(method: str, host: str, path: str, query: str = "") -> dict[str, str]:
    """AWS SigV4 headers for a GET of a private object.

    The canonical request is built exactly as AWS specifies: the headers block
    ends with a single newline, and the newline that joins it to the signed
    header list is supplied by the join. Adding a second newline, or seeding
    the signing key with the timestamp instead of the date, both produce a
    signature the store cannot reproduce.
    """
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{REGION}/s3/aws4_request"
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_headers = (
        f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    )
    signed_header_names = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, path, query, canonical_headers, signed_header_names, payload_hash]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    # kDate -> kRegion -> kService -> kSigning, seeded with the bare date.
    key = _hmac(("AWS4" + SECRET_KEY).encode(), date)
    for part in (REGION, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/{scope}, "
            f"SignedHeaders={signed_header_names}, Signature={signature}"
        ),
    }


def proxy_id(endpoint: str) -> str:
    """A stable, non-reversible ID for an endpoint.

    The ID is what reaches the browser and what Xray routing rules reference,
    so it must be deterministic without exposing the URL itself.
    """
    return "px-" + hashlib.sha256(endpoint.strip().encode()).hexdigest()[:16]


def parse_catalog(raw: str) -> list[dict[str, Any]]:
    """Parse the repository payload into normalized records.

    Two payload shapes are accepted, because repositories differ:

    * a plain text list, one proxy per line, where the line is
      ``protocol://host:port#Country Name - 69%``. The fragment carries the
      country and an optional percentage that is ignored (health is measured
      here, not trusted from the file);
    * a JSON array of objects with ``url``/``country`` keys, or of bare URL
      strings.
    """
    text = (raw or "").strip()
    if not text:
        return []

    records: list[Any] = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("proxies") or data.get("items") or []
        if isinstance(data, list):
            records = data
    if not records:
        records = [line for line in text.splitlines() if line.strip()]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records[:CACHE_MAX]:
        endpoint, raw_country = _extract_endpoint(item)
        if not endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() not in SUPPORTED_SCHEMES or not parsed.hostname:
            continue
        endpoint = endpoint.strip()
        pid = proxy_id(endpoint)
        if pid in seen:
            continue
        seen.add(pid)
        # Repositories spell the country in several ways ("FI", "Finland",
        # "Netherlands - 33%"); normalize_country resolves all of them and
        # never invents a code for a name it does not know.
        _name, code = countries.normalize_country(raw_country)
        out.append(
            {
                "id": pid,
                "code": code,
                "country": (
                    countries.country_name(code)
                    if code
                    else (str(raw_country).strip()[:60] or "Unknown")
                ),
                "flag": countries.flag_for(code),
                # The endpoint stays server-side. The browser only ever sees
                # the id, flag, country, and health.
                "_endpoint": endpoint,
            }
        )
    return out


# What Xray can dial: an HTTP CONNECT proxy (with or without TLS to the proxy
# itself) or a SOCKS5 proxy.
SUPPORTED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

# Splits "https://1.2.3.4:443#Netherlands - 33%" into the endpoint and the
# country. The country may itself contain spaces, dashes, and a percentage, so
# only the first dash is treated as a separator.
_FRAGMENT_SPLIT = re.compile(r"\s+-\s+")


def _extract_endpoint(item: Any) -> tuple[str, str]:
    """Return (endpoint, raw country) from one repository entry."""
    if isinstance(item, dict):
        endpoint = ""
        for key in ("url", "endpoint", "proxy", "address", "uri"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                endpoint = value.strip()
                break
        country = item.get("country") or item.get("code") or ""
        return _split_fragment(endpoint, country)

    if not isinstance(item, str):
        return "", ""

    line = item.strip()
    if not line:
        return "", ""
    # A JSON-ish line inside a text file still parses as a bare URL, which is
    # what we want: anything before the fragment is the endpoint.
    return _split_fragment(line, "")


def _split_fragment(endpoint: str, country: Any) -> tuple[str, str]:
    """Peel the ``#country`` fragment off a proxy line."""
    if "#" not in endpoint:
        return endpoint, country
    head, _, tail = endpoint.partition("#")
    # "Netherlands - 33%" -> ("Netherlands", "33%"). The percentage is dropped:
    # health is measured here rather than trusted from the file.
    name = _FRAGMENT_SPLIT.split(tail, maxsplit=1)[0].strip()
    return head.strip(), (name or country or "")


async def fetch_catalog() -> int:
    """Refresh the catalog. Returns the number of records loaded.

    Signed first, plain second. Some S3-compatible stores keep the bucket
    readable without credentials, and a bucket that is public answers a signed
    request just as happily as an unsigned one — so an unsigned retry costs
    nothing and covers both cases.
    """
    global _catalog, _last_fetch, _last_error, _unsigned_works
    if not BASE_URL:
        _last_error = "no repository configured"
        return len(_catalog)
    parts = urlsplit(BASE_URL)
    host = parts.netloc
    path = parts.path or "/"
    # A query string is part of the canonical request, so it has to be signed
    # in sorted order even though nothing is appended to it here.
    query = "&".join(
        sorted(
            f"{key}={value}"
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        )
    )
    headers = signed_headers("GET", host, path, query)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(BASE_URL, headers=headers)
            # A store that rejected the signature may still serve the object
            # publicly. Retry once without credentials before giving up.
            if response.status_code in (401, 403):
                unsigned = await client.get(BASE_URL)
                if unsigned.status_code == 200:
                    _unsigned_works = True
                    response = unsigned
            response.raise_for_status()
            loaded = parse_catalog(response.text)
        if not loaded:
            _last_error = "repository returned no usable proxies"
            return len(_catalog)
        async with _lock:
            _catalog = loaded
            # Drop results for proxies that left the repository.
            live = {record["id"] for record in loaded}
            for pid in list(_results):
                if pid not in live:
                    _results.pop(pid, None)
            for code in list(_preferred):
                if _preferred[code] not in live:
                    _preferred.pop(code, None)
            # Config assignments survive a repository refresh: the config still
            # exists even if its proxy temporarily vanished, and it comes back
            # on the next fetch.
        _last_fetch = time.time()
        _last_error = ""
        logger.info("proxy catalog refreshed: %d records", len(loaded))
        return len(loaded)
    except Exception as exc:
        # Keep the last good list so the dashboard stays usable.
        _last_error = str(exc)
        logger.warning("proxy catalog refresh failed: %s", exc)
        return len(_catalog)


# ── Probing ──────────────────────────────────────────────────────────────────


async def probe(endpoint: str) -> dict[str, Any]:
    """Test one proxy against both targets, exactly as configured.

    The probe uses the same path a real connection takes, so a proxy that only
    works for some traffic is reported as unhealthy rather than silently
    selected.
    """
    checks: list[dict[str, Any]] = []
    for target in PROBE_TARGETS:
        started = time.monotonic()
        try:
            ok, detail = await _probe_once(endpoint, target)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:200]
        checks.append(
            {
                "target": target,
                "ok": ok,
                "detail": detail,
                "total_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
    healthy = all(check["ok"] for check in checks)
    sample = sum(1 for check in checks if check["ok"])
    previous = _results.get(proxy_id(endpoint)) or {}
    samples = min(int(previous.get("sample_count") or 0) + 1, 10000)
    successes = min(int(previous.get("success_count") or 0) + sample, samples)
    return {
        "proxy_id": proxy_id(endpoint),
        "overall_status": "healthy" if healthy else "unhealthy",
        "checks": checks,
        "sample_count": samples,
        "success_count": successes,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def _probe_once(endpoint: str, target: str) -> tuple[bool, str]:
    """One CONNECT-style probe through the proxy."""
    parsed = urlsplit(endpoint)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme in ("https", "socks5", "socks5h") else 80)
    if not host:
        return False, "no host in proxy url"

    if parsed.scheme in ("socks5", "socks5h"):
        return await _probe_socks(host, port, target)

    connect_host = host
    connect_port = port
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context), timeout=HANDSHAKE_TIMEOUT
        )
    else:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=HANDSHAKE_TIMEOUT
        )
    try:
        dest = urlsplit(target)
        request = (
            f"CONNECT {dest.hostname}:443 HTTP/1.1\r\n"
            f"Host: {dest.hostname}:443\r\n"
        )
        if parsed.username or parsed.password:
            import base64

            token = base64.b64encode(
                f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}".encode()
            ).decode()
            request += f"Proxy-Authorization: Basic {token}\r\n"
        request += "\r\n"
        writer.write(request.encode())
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), timeout=HANDSHAKE_TIMEOUT)
        parts = status_line.decode("latin-1", "replace").split()
        if len(parts) < 2 or parts[1] != "200":
            return False, f"proxy answered {status_line.decode('latin-1', 'replace').strip()[:80]}"
        return True, "tunnel established"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _probe_socks(host: str, port: int, target: str) -> tuple[bool, str]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=HANDSHAKE_TIMEOUT
    )
    try:
        writer.write(b"\x05\x01\x00")  # SOCKS5, one method: no auth
        await writer.drain()
        if await asyncio.wait_for(reader.readexactly(2), timeout=HANDSHAKE_TIMEOUT) != b"\x05\x00":
            return False, "socks5 server rejected the no-auth method"
        dest = urlsplit(target)
        request = b"\x05\x01\x00\x03" + bytes([len(dest.hostname or "")]) + (dest.hostname or "").encode()
        request += (443).to_bytes(2, "big")
        writer.write(request)
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(4), timeout=HANDSHAKE_TIMEOUT)
        if reply[1] != 0x00:
            return False, f"socks5 connect failed with code {reply[1]}"
        return True, "tunnel established"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def probe_all(records: list[dict[str, Any]], limit: int = 40) -> int:
    """Probe many proxies with bounded concurrency."""
    semaphore = asyncio.Semaphore(4)
    targets = [record for record in records if record.get("_endpoint")][:limit]

    async def one(record: dict[str, Any]):
        async with semaphore:
            result = await probe(record["_endpoint"])
            async with _lock:
                _results[record["id"]] = result

    if targets:
        await asyncio.gather(*(one(record) for record in targets))
    return len(targets)


# ── Routing plan ────────────────────────────────────────────────────────────
#
# The panel owns the inbound clients, so it never rewrites the rest of the core
# config. Routing is therefore applied the other way round: this service owns
# the outbounds and the routing rules, and the panel merges them into whatever
# config it builds. Each rule is keyed on the *user email* the panel assigned
# to a config, which is exactly how Xray matches traffic to a user.


def _healthy_endpoint(pid: str) -> str | None:
    """The endpoint behind a proxy ID, if it is currently known and healthy."""
    if not pid:
        return None
    for record in _catalog:
        if record["id"] == pid:
            result = _results.get(pid) or {}
            # An untested proxy is still usable: the plan is advisory and the
            # operator may select a country before the first scan finishes.
            status = result.get("overall_status")
            if status == "unhealthy":
                return None
            return record.get("_endpoint")
    return None


def outbound_tag(pid: str) -> str:
    """Xray outbound tag for a proxy ID. Tags are short and DNS-label safe."""
    return "px-" + hashlib.sha256(pid.encode()).hexdigest()[:12]


def _proxy_outbound(endpoint: str) -> dict[str, Any]:
    """Build one Xray outbound for a proxy endpoint.

    The scheme picks the outbound protocol: socks5 endpoints become a ``socks``
    outbound, http/https endpoints an ``http`` outbound. Guessing socks for
    everything would silently misroute every plain HTTP proxy.
    """
    parsed = urlsplit(endpoint)
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 8080 if scheme == "http" else 1080)
    server: dict[str, Any] = {"address": parsed.hostname or "", "port": port}
    if scheme == "socks5" or scheme == "socks5h":
        users = _socks_users(endpoint)
        if users:
            server["users"] = users
    else:
        # Xray's http outbound takes credentials in this shape.
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if user or password:
            server["user"] = user
            server["pass"] = password
    return {
        "protocol": "socks" if scheme in ("socks5", "socks5h") else "http",
        "settings": {"servers": [server]},
        "streamSettings": _proxy_stream(endpoint),
    }


def route_plan() -> dict[str, Any]:
    """Outbounds and routing rules for the preferred proxy of each country.

    Rules are keyed on a *reserved Xray user email* rather than on the real
    client UUIDs, because this service does not know which configs exist. The
    panel adds that reserved user to a config's inbound client list when the
    operator assigns the config to a country; Xray then matches that user's
    traffic to the proxy outbound.

    When the proxy is unreachable the connection fails: there is no second
    outbound to fall back to, which is the fail-closed contract.
    """
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    skipped: list[str] = []

    for code, pid in sorted(_preferred.items()):
        endpoint = _healthy_endpoint(pid)
        if not endpoint:
            skipped.append(code)
            continue
        tag = outbound_tag(pid)
        outbound = _proxy_outbound(endpoint)
        outbound["tag"] = tag
        outbounds.append(outbound)
        rules.append(
            {
                "type": "field",
                "user": [email_for_user(pid)],
                "outboundTag": tag,
                "comment": f"lumen preferred proxy for {code}",
            }
        )

    return {"outbounds": outbounds, "rules": rules, "skipped": skipped}


def _socks_users(endpoint: str) -> list[dict[str, Any]]:
    """SOCKS credentials, only when the endpoint actually has them."""
    parsed = urlsplit(endpoint)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not user and not password:
        return []
    return [{"user": user, "pass": password, "level": 0}]


def _proxy_stream(endpoint: str) -> dict[str, Any]:
    """Transport settings for the proxy outbound.

    An https:// proxy endpoint means TLS to the proxy itself; a plain http:// or
    socks5:// endpoint connects without it. Xray's socks outbound only speaks
    plain TCP to the proxy, so the scheme is decided here rather than guessed
    later.
    """
    scheme = urlsplit(endpoint).scheme.lower()
    if scheme == "https":
        return {"network": "tcp", "security": "tls"}
    return {"network": "tcp", "security": "none"}


def plan_for_country(code: str) -> dict[str, Any]:
    """The outbound and reserved email one country needs.

    Used by the panel when it assigns a single config to a country, so the
    per-config case and the per-country preference cannot drift apart.
    """
    pid = _preferred.get(str(code or "").strip().upper()[:2])
    if not pid:
        return {}
    endpoint = _healthy_endpoint(pid)
    if not endpoint:
        return {}
    tag = outbound_tag(pid)
    outbound = _proxy_outbound(endpoint)
    outbound["tag"] = tag
    return {
        "outbound": outbound,
        "outboundTag": tag,
        "user": email_for_user(pid),
    }


def email_for_user(pid: str) -> str:
    """Reserved Xray user email used to carry a per-country proxy choice."""
    return "lumen-px-" + hashlib.sha256(pid.encode()).hexdigest()[:10]


# ── Persistence ──────────────────────────────────────────────────────────────


async def save() -> None:
    """Persist results, preferences and assignments.

    Endpoints are never written to disk: they are re-read from the repository
    on the next fetch, and the IDs in ``preferred``/``assignments`` are enough
    to find them again.
    """
    payload = {
        "results": _results,
        "preferred": _preferred,
        "assignments": _assignments,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STORE)


def load() -> None:
    global _results, _preferred, _assignments
    try:
        payload = json.loads(STORE.read_text(encoding="utf-8"))
        results = payload.get("results")
        preferred = payload.get("preferred")
        assignments = payload.get("assignments")
        if isinstance(results, dict):
            _results = {k: v for k, v in results.items() if isinstance(v, dict)}
        if isinstance(preferred, dict):
            _preferred = {str(k): str(v) for k, v in preferred.items()}
        if isinstance(assignments, dict):
            _assignments = {
                str(k): str(v).strip().upper()[:2] for k, v in assignments.items()
            }
    except Exception:
        pass


# ── HTTP API (loopback only) ─────────────────────────────────────────────────


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Strip the endpoint before anything leaves the process."""
    result = _results.get(record["id"]) or {}
    checks = result.get("checks") or []
    total = len(PROBE_TARGETS)
    passed = sum(1 for check in checks if isinstance(check, dict) and check.get("ok"))
    return {
        "id": record["id"],
        "code": record.get("code", ""),
        "country": record.get("country", "Unknown"),
        "flag": record.get("flag", "🏳️"),
        "healthy": bool(checks) and passed == total,
        "health_percent": int(round(100 * passed / total)) if checks else 0,
        "checked_at": result.get("checked_at", ""),
    }


class ProxyHandler(BaseHTTPRequestHandler):
    """Endpoints consumed by the dashboard add-on.

    Authorization is enforced by nginx: only requests carrying the admin bearer
    token are routed here. The handler additionally requires the shared secret
    so the port is not usable if it is ever reached directly.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _deny(self, code: int, detail: str):
        self._send(code, {"detail": detail})

    def _authorized(self) -> bool:
        if not MANUAL_REFRESH_KEY:
            return True
        supplied = self.headers.get("x-lumen-key", "")
        return hmac.compare_digest(supplied, MANUAL_REFRESH_KEY)

    def _send(self, code: int, body: Any):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = self.path.split("?")[0].rstrip("/") or "/"
        if route == "/proxy/catalog":
            records = [public_record(record) for record in _catalog]
            records.sort(key=lambda r: (not r["healthy"], -r["health_percent"], r["country"]))
            self._send(
                200,
                {
                    "proxies": records,
                    "count": len(records),
                    "healthy_count": sum(1 for r in records if r["healthy"]),
                    "last_refresh": _last_fetch,
                    "last_error": _last_error,
                },
            )
            return
        if route == "/proxy/preferred":
            self._send(200, {"preferred": _preferred})
            return
        if route == "/proxy/status":
            self._send(
                200,
                {
                    "configured": bool(BASE_URL),
                    "catalog_size": len(_catalog),
                    "refresh_seconds": REFRESH_SECONDS,
                    "last_refresh": _last_fetch,
                    "last_error": _last_error,
                },
            )
            return
        if route == "/proxy/plan":
            # Consumed by the panel's bootstrap, never by the browser: it
            # carries proxy endpoints, so it stays on the loopback.
            self._send(200, route_plan())
            return
        if route == "/proxy/assignments":
            self._send(200, {"assignments": _assignments})
            return
        self._deny(404, "not found")

    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/") or "/"
        if not self._authorized():
            self._deny(401, "invalid key")
            return
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if route == "/proxy/refresh":
            asyncio.run_coroutine_threadsafe(fetch_catalog(), LOOP).result(timeout=60)
            self._send(200, {"ok": True, "count": len(_catalog)})
            return
        if route == "/proxy/test":
            pid = str(body.get("id") or "")
            record = next((r for r in _catalog if r["id"] == pid), None)
            if record is None:
                self._deny(404, "unknown proxy")
                return
            result = asyncio.run_coroutine_threadsafe(probe(record["_endpoint"]), LOOP).result(timeout=30)
            async def store():
                async with _lock:
                    _results[pid] = result
                await save()
            asyncio.run_coroutine_threadsafe(store(), LOOP).result(timeout=10)
            self._send(200, public_record(record))
            return
        if route == "/proxy/test-all":
            asyncio.run_coroutine_threadsafe(
                probe_all(list(_catalog), int(body.get("limit") or 40)), LOOP
            ).result(timeout=300)
            asyncio.run_coroutine_threadsafe(save(), LOOP).result(timeout=10)
            self._send(200, {"ok": True, "tested": len(_results)})
            return
        if route == "/proxy/preferred":
            code = str(body.get("country") or "").strip().upper()[:2]
            pid = str(body.get("id") or "")
            if not code or not pid:
                self._deny(400, "country and id are required")
                return
            async def store():
                async with _lock:
                    _preferred[code] = pid
                await save()
            asyncio.run_coroutine_threadsafe(store(), LOOP).result(timeout=10)
            self._send(200, {"ok": True, "preferred": _preferred})
            return
        if route == "/proxy/assign":
            # Route one specific config through one country. The panel reads
            # this and adds the matching reserved client to the config.
            username = str(body.get("username") or "").strip()[:64]
            country = str(body.get("country") or "").strip().upper()[:2]
            if not username:
                self._deny(400, "username is required")
                return
            async def store():
                async with _lock:
                    if country:
                        _assignments[username] = country
                    else:
                        _assignments.pop(username, None)
                await save()
            asyncio.run_coroutine_threadsafe(store(), LOOP).result(timeout=10)
            self._send(200, {"ok": True, "assignments": _assignments})
            return
        self._deny(404, "not found")


LOOP: asyncio.AbstractEventLoop


async def refresh_loop() -> None:
    """Re-fetch the repository on a slow schedule.

    A zero interval disables scheduled refreshes; the manual button in the
    dashboard still works.
    """
    if REFRESH_SECONDS <= 0:
        logger.info("scheduled proxy refresh disabled")
        return
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        await fetch_catalog()
        await save()


async def main() -> None:
    global LOOP
    LOOP = asyncio.get_running_loop()
    load()
    if BASE_URL:
        await fetch_catalog()
    else:
        logger.info("no proxy repository configured; the proxy page will show setup instructions")
    asyncio.create_task(refresh_loop())

    server = ThreadingHTTPServer(("127.0.0.1", API_PORT), ProxyHandler)
    server.daemon_threads = True
    logger.info("proxy service on 127.0.0.1:%d", API_PORT)
    await asyncio.get_running_loop().run_in_executor(None, server.serve_forever)


if __name__ == "__main__":
    asyncio.run(main())