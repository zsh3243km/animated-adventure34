"""Lumen bootstrap: zero-touch setup for PasarGuard on Railway.

Idempotent: safe on every boot. Creates the core, node, groups, hosts,
templates, the reseller role, and then watches all of them and repairs drift.
"""
import json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8000"
DATA = os.getenv("LUMEN_DATA", "/var/lib/lumen")
DOMAIN = (os.getenv("PUBLIC_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
import subprocess

USER, PASS = "admin", "admin"          # first-boot login; change it later
RESELLER_USER, RESELLER_PASS, RESELLER_GB = "reseller", "reseller", 50
CORE_NAME, NODE_NAME = "Lumen-Core", "Lumen-Core"
PRO_GROUP, STD_GROUP = "Lumen Pro", "Lumen"
OLD_GROUP = "lumen-all"                        # renamed in an earlier build
TITLE = os.getenv("CONFIG_TITLE", "Lumen")
GB = 1024 ** 3
DAY = 86400

# Every config goes through Railway's TLS edge on 443 with alpn=http/1.1 (the
# only thing Railway serves).
#
# Lumen Pro: the single most compatible + lowest-latency setup, VLESS +
# WebSocket + early data with a Chrome fingerprint.
# Lumen: three configs that genuinely differ (protocol / transport /
# fingerprint / path), all supported by v2rayNG, V2Box, Hiddify, Streisand,
# NekoBox, Happ, Clash Meta and sing-box.
#
# ?ed=2560 = early data: the first packet rides the handshake, saving one round
# trip per connection.
INBOUNDS = [
    # tag            proto    port  net            default path    fp         name     group
    ("LM-VLESS-WS-1", "vless",  10001, "ws",          "/lumen/ws/",     "chrome",  "Lumen",  "pro"),
    ("LM-VLESS-WS-2", "vless",  10002, "ws",          "/lumen/stream/", "firefox", "Stream", "std"),
    ("LM-VMESS-WS",   "vmess",  10004, "ws",          "/lumen/vmess/",  "edge",    "Diamond", "std"),
    ("LM-VLESS-HU",   "vless",  10005, "httpupgrade", "/lumen/hu/",     "ios",     "Night",  "std"),
]
EARLY_DATA = "?ed=2560"

# Real paths are unique per install (genpaths.py writes them to the volume
# before nginx starts).
try:
    with open(f"{DATA}/paths.json") as f:
        _P = json.load(f)
    INBOUNDS = [
        (tag, proto, port, net, _P.get(tag, path), fp, name, grp)
        for (tag, proto, port, net, path, fp, name, grp) in INBOUNDS
    ]
except Exception as exc:
    print("[bootstrap] paths.json missing, run genpaths.py first:", exc, flush=True)
    sys.exit(1)

TEMPLATES = [  # name, GB, days, group
    ("10GB - 30 days", 10, 30, "std"), ("30GB - 30 days", 30, 30, "std"),
    ("50GB - 30 days", 50, 30, "std"), ("100GB - 30 days", 100, 30, "std"),
    ("200GB - 60 days", 200, 60, "std"), ("Unlimited - 30 days", 0, 30, "std"),
    ("Pro 30GB - 30 days", 30, 30, "pro"), ("Pro 50GB - 50 days", 50, 50, "pro"),
    ("Pro 100GB - 100 days", 100, 100, "pro"), ("Pro Unlimited - 30 days", 0, 30, "pro"),
]

TOKEN = None
_QUIET = {}


def log(*args):
    print("[bootstrap]", *args, flush=True)


_GLOBAL = object()


def req(method, path, body=None, form=False, ok=(200, 201, 204), token=_GLOBAL):
    url = BASE + path
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    tok = TOKEN if token is _GLOBAL else token
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw) if raw.strip() else None
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="ignore")
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, text
    except Exception:
        return 0, None


def req_anon(method, path, body=None, form=False):
    return req(method, path, body, form=form, token=None)


def must(method, path, body=None):
    code, res = req(method, path, body)
    if code not in (200, 201, 204):
        raise RuntimeError(f"{method} {path} -> {code}: {res}")
    return res


def as_list(res, key):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get(key) or []
    return []


def wait_panel():
    for _ in range(180):
        try:
            code, _ = req("GET", "/api/system")
            if code in (200, 401, 403):
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("panel did not come up")


# ── Owner / account handling, straight against the panel database ────────────

ADMIN_PW_PY = """
import asyncio, sys
from app.db.base import GetDB
from app.db.crud.admin import get_owner, get_admin
from app.models.admin import _hash_password_sync

async def main():
    mode, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    async with GetDB() as db:
        if mode == "owner":
            account = await get_owner(db)
        else:
            account = await get_admin(db, user, load_users=False, load_usage_logs=False)
        if account is None:
            print("MISSING")
            return
        account.username = user
        account.hashed_password = _hash_password_sync(pw)
        await db.commit()
        print("OK")

asyncio.run(main())
"""


def write_password(mode, user, pw):
    out = subprocess.run(
        [sys.executable, "-c", ADMIN_PW_PY, mode, user, pw],
        cwd="/code", capture_output=True, text=True, timeout=90,
    )
    ok = out.stdout.strip().splitlines()[-1:] == ["OK"]
    if not ok:
        log(f"password write ({mode} {user}) failed:", (out.stdout + out.stderr)[-400:])
    return ok


ADMIN_DB_PY = r"""
import asyncio, json, sys

req = json.loads(sys.argv[1])

def _hash(pw):
    try:
        from app.models.admin import _hash_password_sync
        return _hash_password_sync(pw)
    except Exception:
        import bcrypt
        return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def _check(pw, h):
    if not h:
        return False
    try:
        import bcrypt
        return bcrypt.checkpw(pw.encode(), h.encode())
    except Exception:
        try:
            from passlib.context import CryptContext
            return CryptContext(schemes=["bcrypt"]).verify(pw, h)
        except Exception:
            return None

async def main():
    from sqlalchemy import select, func
    from app.db.base import GetDB
    from app.db.models import Admin

    out = {"ok": False}
    async with GetDB() as db:
        async def by_name(n):
            r = await db.execute(select(Admin).where(func.lower(Admin.username) == n.strip().lower()))
            return r.scalars().first()

        def is_owner(a):
            try:
                role = getattr(a, "role", None)
                if role is not None and getattr(role, "is_owner", False):
                    return True
            except Exception:
                pass
            return bool(getattr(a, "is_sudo", False) and getattr(a, "is_owner", True))

        op = req["op"]
        if op == "verify":
            a = await by_name(req["username"])
            if a is not None:
                c = _check(req["password"], a.hashed_password)
                out = {"ok": c is True, "unknown": c is None, "username": a.username, "owner": is_owner(a)}
        elif op == "set":
            a = await by_name(req["username"])
            if a is None:
                out = {"ok": False, "error": "missing"}
            else:
                new_name = (req.get("new_username") or "").strip()
                if new_name and new_name.lower() != a.username.lower():
                    other = await by_name(new_name)
                    if other is not None:
                        print("RESULT=" + json.dumps({"ok": False, "error": "taken"}))
                        return
                    a.username = new_name
                a.hashed_password = _hash(req["password"])
                await db.commit()
                await db.refresh(a)
                out = {"ok": _check(req["password"], a.hashed_password) is not False, "username": a.username}
    print("RESULT=" + json.dumps(out))

asyncio.run(main())
"""


def admin_db(**request):
    """Talk to the panel database directly (bcrypt). Never raises."""
    try:
        out = subprocess.run(
            [sys.executable, "-c", ADMIN_DB_PY, json.dumps(request)],
            cwd="/code", capture_output=True, text=True, timeout=90,
        )
        for line in out.stdout.splitlines():
            if line.startswith("RESULT="):
                return json.loads(line[7:])
        log("account tool:", (out.stdout + out.stderr)[-300:])
    except Exception as exc:
        log("account tool error:", exc)
    return {"ok": False, "error": "tool"}


# ── Owner access ─────────────────────────────────────────────────────────────

MARKER = f"{DATA}/.owner_initialized"
RESELLER_MARKER = f"{DATA}/.reseller_initialized"

TEMP_KEY_PY = """
import asyncio
from app.db.base import GetDB
from app.db.crud.temp_key import create_temp_key

async def main():
    async with GetDB() as db:
        key = await create_temp_key(db)
        print("KEY=" + key.key)

asyncio.run(main())
"""


def temp_key():
    out = subprocess.run(
        [sys.executable, "-c", TEMP_KEY_PY],
        cwd="/code", capture_output=True, text=True, timeout=60,
    )
    for line in out.stdout.splitlines():
        if line.startswith("KEY="):
            return line[4:].strip()
    raise RuntimeError(f"temp key failed: {out.stderr[-500:]}")


def strong_tmp():
    import secrets
    return "Lm" + secrets.token_hex(8) + "Aa9!Zz7"


TOKEN_PY = """
import asyncio
from app.db.base import GetDB
from app.db.crud.admin import get_owner
from app.utils.jwt import create_admin_token

async def main():
    async with GetDB() as db:
        owner = await get_owner(db)
        if owner is None:
            print("NOOWNER")
            return
        print("TOKEN=" + await create_admin_token(owner.id, owner.username))

asyncio.run(main())
"""


def owner_token():
    out = subprocess.run(
        [sys.executable, "-c", TOKEN_PY],
        cwd="/code", capture_output=True, text=True, timeout=60,
    )
    for line in out.stdout.splitlines():
        if line.startswith("TOKEN="):
            return line[6:].strip()
        if line.strip() == "NOOWNER":
            return None
    raise RuntimeError(f"token failed: {(out.stdout + out.stderr)[-400:]}")


def ensure_owner():
    """First boot: owner = admin/admin. After that the password is yours."""
    if owner_token() is None:
        code, res = req("POST", "/api/setup/owner", {
            "key": temp_key(), "username": "lumenowner",
            "password": strong_tmp(),
        })
        if code not in (200, 201, 409):
            log(f"owner create failed {code}: {res}")
        if os.path.exists(MARKER):
            os.remove(MARKER)
    if not os.path.exists(MARKER):
        if write_password("owner", USER, PASS):
            open(MARKER, "w").write(str(int(time.time())))
            log("owner ready:", USER, "/", PASS, "(first boot)")


_owner_keys = {}            # key -> expiry (memory only, one use, 5 minutes)
KEY_TTL = 300


def new_owner_key():
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I: easy to type
    key = "LM-" + "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
    now = time.time()
    for old in [x for x, exp in _owner_keys.items() if exp < now]:
        _owner_keys.pop(old, None)
    _owner_keys[key] = now + KEY_TTL
    return key


def use_owner_key(key):
    key = str(key or "").strip().upper()
    exp = _owner_keys.pop(key, None)
    return bool(exp and exp >= time.time())


def print_owner_key():
    key = new_owner_key()
    log("=" * 60)
    log("OWNER KEY (valid 5 min, one use):", key)
    log("login page > Owner access > paste the key > set a new username and password")
    log("need a new key? Restart the service in Railway")
    log("=" * 60)


def login():
    global TOKEN
    ensure_owner()
    for _ in range(30):
        tok = owner_token()
        if tok:
            TOKEN = tok
            return
        time.sleep(3)
    raise RuntimeError("could not get an owner token")


# ── Core, node, groups, hosts, settings, templates ───────────────────────────


def inbound(tag, proto, port, net, path):
    stream = {"network": net, "security": "none"}
    if net == "ws":
        stream["wsSettings"] = {"path": path}
    elif net == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path}
    elif net == "xhttp":
        stream["xhttpSettings"] = {"path": path, "mode": "auto"}
    settings = {"clients": []}
    if proto == "vless":
        settings["decryption"] = "none"
    return {
        "tag": tag, "listen": "127.0.0.1", "port": port, "protocol": proto,
        "settings": settings, "streamSettings": stream,
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True},
    }


PROXY_API = "http://127.0.0.1:8100"


def proxy_plan():
    """Outbounds and routing rules from the proxy service.

    The panel owns inbound clients, so the proxy side is merged in here rather
    than written by the proxy service. Without a reachable proxy service the
    core still builds: traffic simply goes direct.
    """
    try:
        with urllib.request.urlopen(f"{PROXY_API}/proxy/plan", timeout=10) as resp:
            plan = json.loads(resp.read().decode())
        if isinstance(plan, dict):
            return plan
    except Exception:
        pass
    return {"outbounds": [], "rules": [], "skipped": []}


def proxy_assignments():
    """config username -> country code, for configs an operator routed.

    Owned by the proxy service, which the dashboard writes to when an operator
    picks a country for a specific config.
    """
    try:
        with urllib.request.urlopen(f"{PROXY_API}/proxy/assignments", timeout=10) as resp:
            body = json.loads(resp.read().decode())
        if isinstance(body, dict) and isinstance(body.get("assignments"), dict):
            return {str(k): str(v).upper()[:2] for k, v in body["assignments"].items()}
    except Exception:
        pass
    return {}


def core_config():
    """The Xray core config, including per-country proxy routing when set."""
    plan = proxy_plan()
    # Proxy rules come first: a config assigned to a country must leave through
    # that proxy even when its destination happens to be a private address.
    rules = list(plan.get("rules") or []) + [
        {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
        {"type": "field", "protocol": ["bittorrent"], "outboundTag": "BLOCK"},
    ]
    outbounds = list(plan.get("outbounds") or []) + [
        {"protocol": "freedom", "tag": "DIRECT", "settings": {"domainStrategy": "UseIPv4"}},
        {"protocol": "blackhole", "tag": "BLOCK"},
    ]
    if plan.get("skipped"):
        log(f"proxy routing: {len(plan['skipped'])} country/countries skipped, "
            f"selected proxy is missing or unhealthy")
    return {
        "log": {"loglevel": "warning"},
        # System resolver first (fastest inside Railway), DoH as a backup.
        # Xray caches every answer.
        "dns": {
            "servers": ["localhost", "https+local://1.1.1.1/dns-query", "8.8.8.8"],
            "queryStrategy": "UseIPv4",
        },
        "inbounds": [inbound(*i[:5]) for i in INBOUNDS],
        "outbounds": outbounds,
        # AsIs routes without an extra DNS lookup, saving one round trip per
        # new connection.
        "routing": {"domainStrategy": "AsIs", "rules": rules},
        "policy": {"levels": {"0": {
            "handshake": 4, "connIdle": 300, "uplinkOnly": 1, "downlinkOnly": 1,
            "bufferSize": 512,
        }}},
    }


def ensure_core():
    config = core_config()
    cores = as_list(must("GET", "/api/cores"), "cores")
    for core in cores:
        if core.get("name") in (CORE_NAME, "lumen-core"):
            if core.get("name") == CORE_NAME and core.get("config") == config:
                log("core ok (unchanged)", core["id"])
                return core["id"]
            body = {
                "name": CORE_NAME, "config": config,
                "exclude_inbound_tags": [], "fallbacks_inbound_tags": [],
            }
            code, res = req("PUT", f"/api/core/{core['id']}?restart_nodes=true", body)
            if code not in (200, 201):
                # The node may still be starting; save without restarting.
                code, res = req("PUT", f"/api/core/{core['id']}?restart_nodes=false", body)
            log("core updated" if code in (200, 201) else f"core update failed {code}: {res}", core["id"])
            return core["id"]
    created = must("POST", "/api/core", {
        "name": CORE_NAME, "config": config,
        "exclude_inbound_tags": [], "fallbacks_inbound_tags": [],
    })
    log("core created", created["id"])
    return created["id"]


def ensure_node(core_id):
    with open(f"{DATA}/node_api_key") as f:
        api_key = f.read().strip()
    with open(f"{DATA}/node-certs/cert.pem") as f:
        cert = f.read().strip()
    body = {
        "name": NODE_NAME, "address": "127.0.0.1", "port": 62050,
        "usage_coefficient": 1, "connection_type": "grpc", "server_ca": cert,
        "keep_alive": 60, "core_config_id": core_id, "api_key": api_key,
    }
    for node in as_list(must("GET", "/api/nodes"), "nodes"):
        if node.get("name") in (NODE_NAME, "lumen-local"):
            must("PUT", f"/api/node/{node['id']}", body)
            log("node updated")
            return
    must("POST", "/api/node", body)
    log("node created")


def ensure_groups():
    """Two groups: PRO_GROUP -> the 1 Pro config, STD_GROUP -> the other 3."""
    want = {
        "pro": (PRO_GROUP, [i[0] for i in INBOUNDS if i[7] == "pro"]),
        "std": (STD_GROUP, [i[0] for i in INBOUNDS if i[7] == "std"]),
    }
    groups = as_list(must("GET", "/api/groups"), "groups")
    by_name = {g.get("name"): g for g in groups}
    if STD_GROUP not in by_name and OLD_GROUP in by_name:
        # Upgrade: keep the users of the old group.
        by_name[STD_GROUP] = by_name.pop(OLD_GROUP)
    ids = {}
    for key, (name, tags) in want.items():
        group = by_name.get(name)
        if group:
            if group.get("name") != name or sorted(group.get("inbound_tags") or []) != sorted(tags):
                must("PUT", f"/api/group/{group['id']}", {"name": name, "inbound_tags": tags})
                log("group fixed:", name)
            ids[key] = group["id"]
        else:
            group = must("POST", "/api/group", {"name": name, "inbound_tags": tags})
            ids[key] = group["id"]
            log("group created:", name, f"({len(tags)} config)")
    return ids


def _norm(value):
    """Compare host fields whether the API returns a string, a list, or an enum."""
    if isinstance(value, list):
        return [str(x).lower() for x in value]
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and "," not in value:
        return value.lower()
    return str(value).lower()


def ensure_hosts():
    if not DOMAIN:
        log("WARNING: no public domain yet (Settings > Networking > Generate Domain), hosts skipped")
        return
    existing = as_list(must("GET", "/api/hosts"), "hosts")
    wanted = {i[0] for i in INBOUNDS}
    for host in existing:
        # Clean hosts left from an earlier Lumen build.
        tag = str(host.get("inbound_tag") or "")
        if tag.startswith("LM-") and tag not in wanted:
            req("DELETE", f"/api/host/{host['id']}")
    changed = 0
    for idx, (tag, proto, port, net, path, fp, name, grp) in enumerate(INBOUNDS):
        body = {
            "remark": f"{name} | {TITLE}", "allowinsecure": False,
            "address": [DOMAIN], "inbound_tag": tag, "port": 443, "sni": [DOMAIN],
            "host": [DOMAIN], "path": path + EARLY_DATA, "security": "tls",
            "alpn": ["http/1.1"], "fingerprint": fp, "priority": idx + 1,
            "is_disabled": False,
        }
        mine = [h for h in existing if h.get("inbound_tag") == tag]
        if mine:
            current = mine[0]
            if any(_norm(current.get(k)) != _norm(v) for k, v in body.items()):
                must("PUT", f"/api/host/{current['id']}", {**body, "id": current["id"]})
                changed += 1
            for extra in mine[1:]:
                req("DELETE", f"/api/host/{extra['id']}")
        else:
            must("POST", "/api/host/", body)
            changed += 1
    if changed or not _QUIET.get("hosts"):
        log(f"{len(INBOUNDS)} hosts ready on", DOMAIN)
        _QUIET["hosts"] = True


def ensure_settings():
    if not DOMAIN:
        return
    code, settings = req("GET", "/api/settings")
    if code != 200 or not isinstance(settings, dict) or "subscription" not in settings:
        log("settings endpoint not as expected, skipped")
        return
    sub = settings["subscription"]
    want = {
        "url_prefix": f"https://{DOMAIN}",
        "profile_title": TITLE,
        "update_interval": 12,
    }
    if all(sub.get(k) == v for k, v in want.items()):
        if not _QUIET.get("settings"):
            log("subscription settings ok")
            _QUIET["settings"] = True
        return
    sub.update(want)
    code, res = req("PUT", "/api/settings", {"subscription": sub})
    log("subscription settings", "ok" if code in (200, 201) else f"skipped ({code})")


RESELLER_ROLE = "Reseller"


def ensure_reseller_role(gids):
    """Reseller role: manages only its own users, must use ready templates."""
    own = {"scope": 1}
    role = {
        "name": RESELLER_ROLE,
        "permissions": {
            "users": {
                "create": own, "read": own, "read_simple": own, "update": own,
                "delete": own, "reset_usage": own, "revoke_sub": own,
                "activate_next_plan": own,
            },
            "templates": {"read": True, "read_simple": True},
            "groups": {"read_simple": True},
            "system": {"read": True},
            "settings": {"read_general": True},
        },
        "access": {"require_template": True, "allowed_group_ids": sorted(gids.values())},
        "disabled_when_limited": True,
    }
    code, res = req("GET", "/api/admin-roles")
    if code != 200:
        log("reseller role endpoint not found, skipped")
        return
    roles = as_list(res, "roles")
    mine = [r for r in roles if r.get("name") == RESELLER_ROLE]
    if mine:
        access = mine[0].get("access") or {}
        if (
            sorted(access.get("allowed_group_ids") or []) != role["access"]["allowed_group_ids"]
            or access.get("require_template") is not True
        ):
            req("PUT", f"/api/admin-role/{mine[0]['id']}", {"access": role["access"]})
        if not _QUIET.get("role"):
            log("reseller role ready")
            _QUIET["role"] = True
        return
    code, res = req("POST", "/api/admin-role", role)
    if code in (200, 201):
        log("reseller role created")
        return
    log(f"reseller role failed {code}: {res}")


def role_id_by_name(name):
    code, res = req("GET", "/api/admin-roles")
    for role in as_list(res, "roles") if code == 200 else []:
        if role.get("name") == name:
            return role["id"]
    return None


def ensure_demo_reseller():
    """A ready reseller account: 50 GB quota, own users only."""
    if os.getenv("DEMO_RESELLER", "on").lower() in ("off", "false", "0", "no"):
        return
    if os.path.exists(RESELLER_MARKER):
        return       # created once; deleting it in the panel is respected
    rid = role_id_by_name(RESELLER_ROLE)
    if not rid:
        log("demo reseller skipped (no role)")
        return
    code, _ = req("POST", "/api/admin/token", {"username": RESELLER_USER, "password": RESELLER_PASS}, form=True)
    if code == 200:
        open(RESELLER_MARKER, "w").write("1")
        return
    code, res = req("POST", "/api/admin", {
        "username": RESELLER_USER, "password": strong_tmp(), "role_id": rid,
        "data_limit": RESELLER_GB * GB, "profile_title": TITLE,
    })
    if code in (200, 201):
        if write_password("user", RESELLER_USER, RESELLER_PASS):
            open(RESELLER_MARKER, "w").write("1")
            log("demo reseller ready:", RESELLER_USER)
    elif code == 409:
        open(RESELLER_MARKER, "w").write("1")   # already exists
    else:
        log(f"demo reseller failed {code}: {res}")


def ensure_templates(gids):
    have = {t.get("name"): t for t in as_list(must("GET", "/api/user_templates"), "templates")}
    for name, gb, days, grp in TEMPLATES:
        body = {
            "name": name, "data_limit": gb * GB, "expire_duration": days * DAY,
            "group_ids": [gids[grp]], "status": "active",
            "data_limit_reset_strategy": "no_reset",
        }
        if name in have:
            if have[name].get("group_ids") != [gids[grp]]:
                req("PUT", f"/api/user_template/{have[name]['id']}", body)
        else:
            must("POST", "/api/user_template", body)
            _QUIET["tpl"] = False
    if not _QUIET.get("tpl"):
        log("user templates ready")
        _QUIET["tpl"] = True


def attach_orphans(gids):
    """Users created without a group go into the main group."""
    code, res = req("GET", "/api/users?no_group=true&limit=200")
    if code == 401:
        login()
        code, res = req("GET", "/api/users?no_group=true&limit=200")
    if code != 200:
        return
    for user in as_list(res, "users"):
        # Only touch users the API clearly reports as having no group.
        if not isinstance(user, dict) or "group_ids" not in user or user.get("group_ids"):
            continue
        code, res = req("PUT", f"/api/user/{user['username']}", {"group_ids": [gids["std"]]})
        log(
            f"no group for {user['username']} -> {STD_GROUP}" if code == 200
            else f"attach {user['username']} failed {code}: {res}"
        )


def apply_proxy_routing():
    """Verify that every assigned config actually reaches Xray.

    The panel owns inbound membership and attaches a user to an inbound through
    ``group_ids``; it never lets a client be edited directly. So a per-config
    reserved client cannot be injected from here, and this function does not try.

    What it does instead is the half that is genuinely ours: confirm that each
    assigned country has a live rule and a reachable outbound in the core config
    that is actually running, and report the ones that do not. Anything else
    would be a guess about a shape this API does not expose.
    """
    assignments = proxy_assignments()
    if not assignments:
        return False

    plan = proxy_plan()
    live_countries = {}
    for rule in plan.get("rules") or []:
        comment = str(rule.get("comment") or "")
        if " for " in comment:
            live_countries[comment.rsplit(" for ", 1)[-1].strip().upper()[:2]] = rule["outboundTag"]

    unrouteable = sorted(
        username
        for username, country in assignments.items()
        if country not in live_countries
    )
    if unrouteable and not _QUIET.get("proxy"):
        log(
            f"proxy routing: {len(unrouteable)} config(s) have no healthy proxy for their "
            f"country and will use the direct route ({', '.join(unrouteable[:5])}"
            f"{'...' if len(unrouteable) > 5 else ''})"
        )
        _QUIET["proxy"] = True
    return False


def heal_node(state):
    """Self-healing: restart the core if it is not connected twice in a row."""
    code, res = req("GET", "/api/nodes")
    if code == 401:
        login()
        code, res = req("GET", "/api/nodes")
    if code != 200:
        return
    for node in as_list(res, "nodes"):
        if node.get("name") != NODE_NAME:
            continue
        status = str(node.get("status") or "")
        if status in ("connected", "disabled"):
            state["bad"] = 0
            return
        state["bad"] = state.get("bad", 0) + 1
        if state["bad"] >= 2:
            log(f"core status '{status}' -> auto-restart")
            code, _ = req("POST", f"/api/core/{node.get('core_config_id')}/restart")
            if code not in (200, 204):
                try:
                    ensure_node(node.get("core_config_id"))
                except Exception as exc:
                    log("node reconnect failed:", exc)
            state["bad"] = 0
        return


RESTART_GAP = 600   # at most one automatic restart every 10 minutes


def restart_panel(reason):
    """Last resort: stop the panel; entrypoint.sh starts it again in a second.

    All data stays on the volume.
    """
    mark = f"{DATA}/.doctor_restart"
    try:
        if time.time() - os.path.getmtime(mark) < RESTART_GAP:
            log("doctor: panel was restarted a moment ago, waiting ->", reason)
            return
    except OSError:
        pass
    open(mark, "w").write(reason)
    log("doctor: restarting the panel ->", reason)
    me = os.getpid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="ignore")
        except Exception:
            continue
        if "main.py" in cmd and "python" in cmd:
            try:
                os.kill(int(pid), 15)
            except Exception:
                pass


# ── Subscription guard ───────────────────────────────────────────────────────

SUB_TEMPLATE = os.getenv("LUMEN_SUB_TEMPLATE", "/code/custom_templates/subscription/index.html")
SUB_PRISTINE = os.getenv("LUMEN_SUB_PRISTINE", "/etc/lumen/sub.html")
HEAL = threading.Lock()          # the doctor and the guard never fix the same thing
_PRISTINE = {}


def guard_template():
    """The subscription page file is missing or changed: put the original back."""
    good = _PRISTINE.get("b")
    if good is None:
        try:
            with open(SUB_PRISTINE, "rb") as f:
                good = _PRISTINE["b"] = f.read()
        except OSError:
            return False
    try:
        with open(SUB_TEMPLATE, "rb") as f:
            current = f.read()
    except OSError:
        current = None
    if current == good:
        return False
    os.makedirs(os.path.dirname(SUB_TEMPLATE), exist_ok=True)
    tmp = SUB_TEMPLATE + ".tmp"
    with open(tmp, "wb") as f:
        f.write(good)
    os.replace(tmp, SUB_TEMPLATE)
    log("sub-guard: subscription page file was", "missing" if current is None else "changed", "-> restored")
    return True


def first_sub_path():
    code, res = req("GET", "/api/users?limit=1")
    if code == 401:
        login()
        code, res = req("GET", "/api/users?limit=1")
    users = as_list(res, "users") if code == 200 else []
    url = (users[0].get("subscription_url") or "") if users and isinstance(users[0], dict) else ""
    if not url:
        return ""
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url


def probe_sub(path, ua):
    """Open a subscription link like a customer: the page or the config list."""
    try:
        request = urllib.request.Request(BASE + path, headers={"User-Agent": ua})
        with urllib.request.urlopen(request, timeout=4) as resp:
            body = resp.read(4096)
            return resp.status < 500 and bool(body)
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def sub_guard():
    """Every 3 s: is the page file intact and does a subscription link answer?

    1st failure -> template restored and subscription settings re-applied
    2nd failure -> the test link is refreshed (the user may have been deleted)
    3rd failure -> the panel is restarted, back in a few seconds
    """
    state, index, said = {"path": "", "t": 0.0, "bad": 0}, 0, {}

    def say(message):
        # Same message at most once a minute, to keep logs clean.
        if time.time() - said.get(message, 0) > 60:
            said[message] = time.time()
            log("sub-guard:", message)

    log("sub-guard on: subscription links checked every 3 s")
    while True:
        try:
            with HEAL:
                guard_template()
            if not state["path"] or time.time() - state["t"] > 60:
                state["path"], state["t"] = first_sub_path(), time.time()
            if state["path"]:
                ua = (
                    "Mozilla/5.0 (iPhone) Lumen-Guard" if index % 2 == 0
                    else "v2rayNG/1.9 Lumen-Guard"
                )
                if probe_sub(state["path"], ua):
                    if state["bad"]:
                        log("sub-guard: subscription links are fine again")
                        said.clear()
                    state["bad"] = 0
                else:
                    state["bad"] += 1
                    say(f"subscription link failed ({state['bad']}/3), fixing")
                    if state["bad"] == 1:
                        with HEAL:
                            guard_template()
                            ensure_settings()
                    elif state["bad"] == 2:
                        state["t"] = 0
                    else:
                        state["bad"] = 0
                        restart_panel("subscription links keep failing")
        except Exception:
            say("waiting for the panel")
        index += 1
        time.sleep(3)


def watch(gids):
    """Doctor loop (15 s tick): find problems and fix them.

    - core/node not connected  -> restart core, reconnect node   (every minute)
    - a group deleted or changed -> re-created / fixed            (every minute)
    - hosts, settings, templates, reseller role  -> fixed         (every 5 minutes)
    """
    log("doctor on: self-heal core, groups, hosts, subscription, templates")
    state, index = {}, 0

    def regroup():
        new = ensure_groups()
        if new:
            gids.update(new)

    jobs = (
        (lambda: attach_orphans(gids), 1),
        (lambda: heal_node(state), 4),
        (regroup, 4),
        (ensure_hosts, 20),
        (ensure_settings, 20),
        (lambda: ensure_templates(gids), 20),
        (lambda: ensure_reseller_role(gids), 20),
        # Proxy routing follows the proxy service, which the operator changes
        # from the dashboard. Checked every 5 minutes so a country change is
        # picked up without a redeploy.
        (apply_proxy_routing, 20),
        # The core config embeds the outbounds, so it is rewritten whenever the
        # preferred proxy for a country changes.
        (ensure_core, 20),
    )
    while True:
        for job, every in jobs:
            if index % every == 0:
                try:
                    with HEAL:
                        job()
                except Exception as exc:
                    log("doctor:", exc)
        index += 1
        time.sleep(15)


# ── Owner key / password service (loopback HTTP, fronted by nginx) ───────────

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_attempts = {}
USERNAME_OK = lambda u: 3 <= len(u) <= 32 and all(ch.isalnum() or ch in "_.-" for ch in u) and u.isascii()


def client_ip(handler):
    """Real visitor IP behind the proxy (first X-Forwarded-For entry)."""
    xff = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return xff or (handler.headers.get("X-Real-IP") or handler.client_address[0]).strip()


def limited(bucket, ip, fail=False):
    """5 wrong tries per visitor per 10 minutes, per feature."""
    key = f"{bucket}:{ip}"
    now = time.time()
    hist = [t for t in _attempts.get(key, []) if now - t < 600]
    if fail:
        hist.append(now)
    _attempts[key] = hist
    if len(_attempts) > 5000:
        _attempts.clear()
    return len(hist) >= 5


class KeyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(405, {"detail": "use POST"})

    def do_POST(self):
        route = self.path.split("?")[0].rstrip("/")
        if route not in ("/lumen/key", "/lumen/password", "/lumen/reset"):
            return self._send(404, {"detail": "not found"})
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(length) or b"{}") or {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if route == "/lumen/reset":
            try:
                return reset_with_key(self, body)
            except Exception as exc:
                log("owner reset error:", exc)
                return self._send(500, {"detail": "internal error, try again"})
        if route == "/lumen/password":
            try:
                return change_password(self, body)
            except Exception as exc:
                log("password change error:", exc)
                return self._send(500, {"detail": "internal error, try again"})

        token = None
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer ") and not body.get("username"):
            token = auth[7:].strip()          # already signed in to the panel
        else:
            ip = client_ip(self)
            if limited("login", ip):
                return self._send(429, {"detail": "too many tries, wait 10 minutes"})
            user = str(body.get("username", ""))[:64]
            password = str(body.get("password", ""))[:128]
            if not user or not password:
                return self._send(401, {"detail": "enter the owner username and password"})
            code, res = req_anon("POST", "/api/admin/token", {"username": user, "password": password}, form=True)
            if code != 200:
                limited("login", ip, fail=True)
                return self._send(401, {"detail": "wrong username or password"})
            token = (res or {}).get("access_token") if isinstance(res, dict) else None
            if not token:
                return self._send(401, {"detail": "wrong username or password"})
        code, me = req_anon("GET", "/api/admin", token=token)
        if code != 200:
            return self._send(401, {"detail": "sign in to the panel again"})
        if not ((me or {}).get("role") or {}).get("is_owner"):
            return self._send(403, {"detail": "only the panel owner can get a key"})
        key = new_owner_key()
        log("owner key issued from the API Keys page for", me.get("username"))
        self._send(200, {"key": key, "ttl": KEY_TTL})


def change_password(handler, body):
    """Settings > Change password.

    POST /lumen/password {username, current, new, new_username?}. Checks the
    CURRENT username and password straight in the panel database (bcrypt), then
    saves the new password and, if given, the new username.
    """
    ip = client_ip(handler)
    if limited("pw", ip):
        return handler._send(429, {"detail": "too many tries, wait 10 minutes"})
    user = str(body.get("username", "")).strip()[:64]
    current = str(body.get("current", ""))[:256]
    new = str(body.get("new", ""))[:256]
    new_user = str(body.get("new_username", "")).strip()[:32]
    if not user or not current:
        return handler._send(400, {"detail": "enter the current username and password"})
    if not new:
        return handler._send(400, {"detail": "enter a new password"})
    if len(new.encode()) > 72:
        return handler._send(400, {"detail": "the new password is too long (max 72 bytes)"})
    if new_user and not USERNAME_OK(new_user):
        return handler._send(400, {"detail": "username: 3 to 32 letters, numbers or _ . -"})

    verified = admin_db(op="verify", username=user, password=current)
    if verified.get("error") == "tool":
        return handler._send(503, {"detail": "the panel database is busy, try again shortly"})
    if not verified.get("ok"):
        limited("pw", ip, fail=True)
        return handler._send(401, {"detail": "current username or password is wrong"})

    result = admin_db(op="set", username=user, password=new, new_username=new_user or None)
    if result.get("error") == "taken":
        return handler._send(409, {"detail": "that username belongs to another account"})
    if not result.get("ok"):
        return handler._send(500, {"detail": "not saved, try again in a few seconds"})
    handler._send(200, {"ok": True, "username": result.get("username")})


def reset_with_key(handler, body):
    """Login page > Owner access. POST /lumen/reset {key, username, password}."""
    ip = client_ip(handler)
    if limited("reset", ip):
        return handler._send(429, {"detail": "too many tries, wait 10 minutes"})
    key = str(body.get("key", "")).strip().upper()
    user = str(body.get("username", ""))[:32]
    password = str(body.get("password", ""))[:256]
    if not key:
        return handler._send(400, {"detail": "enter the owner key"})
    if not USERNAME_OK(user):
        return handler._send(400, {"detail": "username: 3 to 32 letters, numbers or _ . -"})
    if not password or len(password.encode()) > 72:
        return handler._send(400, {"detail": "enter a password (max 72 bytes)"})
    if not use_owner_key(key):
        limited("reset", ip, fail=True)
        return handler._send(401, {"detail": "wrong or expired key, get a new one"})

    if write_password("owner", user, password):
        handler._send(200, {"ok": True, "username": user})
        return
    handler._send(500, {"detail": "not saved, try again in a few seconds"})


def run_owner_service():
    server = ThreadingHTTPServer(("127.0.0.1", 8101), KeyHandler)
    server.daemon_threads = True
    log("owner key service on 127.0.0.1:8101")
    server.serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    wait_panel()
    login()
    core_id = ensure_core()
    ensure_node(core_id)
    gids = ensure_groups()
    ensure_templates(gids)
    ensure_reseller_role(gids)
    ensure_demo_reseller()
    ensure_hosts()
    ensure_settings()

    threading.Thread(target=run_owner_service, daemon=True).start()
    threading.Thread(target=sub_guard, daemon=True).start()
    threading.Thread(target=lambda: watch(gids), daemon=True).start()

    key = new_owner_key()
    log("=" * 60)
    log(f"READY -> {('https://' + DOMAIN + '/dashboard/') if DOMAIN else 'panel is up'}")
    log("first login:", USER, "/", PASS)
    log("OWNER KEY (valid 5 min, one use):", key)
    print_owner_key()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()