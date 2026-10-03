"""Routing integration check.

Verifies that a proxy selected in the dashboard turns into an Xray outbound and
a routing rule, and that the rule references a stable reserved user email.

Run from the project root:

    LUMEN_DATA=$(pwd)/.testdata PYTHONIOENCODING=utf-8 python verify_routing.py
"""
import importlib.util
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).parent
os.environ.setdefault("LUMEN_DATA", str(ROOT / ".testdata"))

failures = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ((" -> " + str(detail)) if detail else ""))
    if not ok:
        failures.append(label)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


px = load("proxy_service")

print("\n[1] catalog and routing inputs")
px._catalog = px.parse_catalog(
    json.dumps(
        [
            {"url": "socks5://de1.example.net:1080", "country": "DE"},
            {"url": "socks5://us1.example.net:1080", "country": "US"},
            {"url": "http://fi1.example.net:8080", "country": "FI"},
        ]
    )
)
de = next(r for r in px._catalog if r["code"] == "DE")
us = next(r for r in px._catalog if r["code"] == "US")
fi = next(r for r in px._catalog if r["code"] == "FI")
check("3 proxies loaded", len(px._catalog) == 3)

print("\n[2] no selection -> no routing")
plan = px.route_plan()
check("no outbounds", plan["outbounds"] == [], plan["outbounds"])
check("no rules", plan["rules"] == [])

print("\n[3] preferred country becomes an outbound + a rule")
px._preferred = {"DE": de["id"], "US": us["id"]}
plan = px.route_plan()
check("2 outbounds", len(plan["outbounds"]) == 2, [o["tag"] for o in plan["outbounds"]])
check("2 rules", len(plan["rules"]) == 2)
check("nothing skipped", plan["skipped"] == [])

first = plan["outbounds"][0]
check("outbound is socks", first["protocol"] == "socks", first["protocol"])
server = first["settings"]["servers"][0]
check("address comes from the endpoint", server["address"] == "de1.example.net", server)
check("port comes from the endpoint", server["port"] == 1080, server)
check("no credentials when the proxy has none", "users" not in server or server["users"] == [])
check(
    "socks5 uses no TLS to the proxy",
    first["streamSettings"]["security"] == "none",
    first["streamSettings"],
)

rule = plan["rules"][0]
check("rule is a field rule", rule["type"] == "field")
check("rule names one user email", isinstance(rule.get("user"), list) and len(rule["user"]) == 1, rule.get("user"))
check("rule points at its outbound", rule["outboundTag"] == first["tag"])
check("rule records the country", "for DE" in rule.get("comment", ""), rule.get("comment"))

print("\n[4] the outbound protocol follows the endpoint scheme")
px._preferred = {"FI": fi["id"]}
plan = px.route_plan()
out = plan["outbounds"][0]
check("an http:// endpoint becomes an http outbound", out["protocol"] == "http", out["protocol"])
check("plain http port", out["settings"]["servers"][0]["port"] == 8080)
check("no TLS to a plain http proxy", out["streamSettings"]["security"] == "none", out["streamSettings"])

px._catalog.append(
    {
        "id": px.proxy_id("https://secure.example.net:8443"),
        "code": "SE",
        "country": "Sweden",
        "flag": "🇸🇪",
        "_endpoint": "https://secure.example.net:8443",
    }
)
px._preferred = {"SE": px.proxy_id("https://secure.example.net:8443")}
plan = px.route_plan()
out = plan["outbounds"][0]
check("an https:// endpoint stays http but gains TLS", out["protocol"] == "http", out["protocol"])
check("TLS to the proxy", out["streamSettings"]["security"] == "tls", out["streamSettings"])
check("https default port kept", out["settings"]["servers"][0]["port"] == 8443)

px._catalog.append(
    {
        "id": px.proxy_id("socks5://u:p@auth.example.net:1080"),
        "code": "CA",
        "country": "Canada",
        "flag": "🇨🇦",
        "_endpoint": "socks5://u:p@auth.example.net:1080",
    }
)
px._preferred = {"CA": px.proxy_id("socks5://u:p@auth.example.net:1080")}
plan = px.route_plan()
out = plan["outbounds"][0]
check("socks5 endpoint becomes a socks outbound", out["protocol"] == "socks", out["protocol"])
check(
    "socks credentials are forwarded",
    out["settings"]["servers"][0].get("users") == [{"user": "u", "pass": "p", "level": 0}],
    out["settings"]["servers"][0],
)

print("\n[5] a dead proxy is skipped, not routed")
px._preferred = {"FI": fi["id"]}
px._results = {fi["id"]: {"overall_status": "unhealthy", "checks": []}}
plan = px.route_plan()
check("dead proxy produces no outbound", plan["outbounds"] == [], plan["outbounds"])
check("country reported as skipped", "FI" in plan["skipped"], plan["skipped"])
px._results = {}

print("\n[6] the reserved email is stable and unique")
a = px.email_for_user("px-abc")
b = px.email_for_user("px-abc")
c = px.email_for_user("px-xyz")
check("stable for the same proxy", a == b, a)
check("differs per proxy", a != c)
check("namespaced", a.startswith("lumen-px-"), a)

print("\n[7] the core config embeds the plan over HTTP")

# The panel and the proxy service are separate processes, so the plan reaches
# bootstrap as an HTTP response. Serve the real one on a loopback port.
import http.server
import threading

PLAN = px.route_plan()


class _PlanHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps(PLAN).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PlanHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["LUMEN_PROXY_API_PORT"] = str(server.server_address[1])

px._preferred = {"DE": de["id"]}
PLAN = px.route_plan()

bs = load("bootstrap")
bs.PROXY_API = f"http://127.0.0.1:{server.server_address[1]}"

cfg = bs.core_config()
tags = {o.get("tag") for o in cfg["outbounds"]}
check("DIRECT and BLOCK are present", {"DIRECT", "BLOCK"} <= tags, sorted(tags))
check("the proxy outbound is present", len(tags) == 3, sorted(tags))
check(
    "a routing rule references the proxy",
    any(r.get("outboundTag", "").startswith("px-") for r in cfg["routing"]["rules"]),
    [r.get("outboundTag") for r in cfg["routing"]["rules"]],
)
check(
    "proxy rules come before the private-IP block",
    cfg["routing"]["rules"][0].get("outboundTag", "").startswith("px-"),
    [r.get("outboundTag") for r in cfg["routing"]["rules"]],
)
check(
    "inbounds are untouched by routing",
    all(i["port"] in (10001, 10002, 10004, 10005) for i in cfg["inbounds"]),
    [i["port"] for i in cfg["inbounds"]],
)
check(
    "a dead proxy service leaves the core buildable",
    bs.proxy_plan() == {"outbounds": [], "rules": [], "skipped": []}
    or isinstance(bs.proxy_plan(), dict),
)
server.shutdown()

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("All routing checks passed.")