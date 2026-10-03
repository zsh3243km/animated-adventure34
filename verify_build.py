"""Integration check for the Lumen build.

Runs the pieces that can be exercised without a live panel: path generation,
core config generation, and the agreement between them. Run from the project
root:

    LUMEN_DATA=$(pwd)/.testdata PYTHONIOENCODING=utf-8 python verify_build.py
"""
import importlib.util
import json
import os
import pathlib
import sys

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


print("\n[1] genpaths")
import subprocess

data = pathlib.Path(os.environ["LUMEN_DATA"])
data.mkdir(parents=True, exist_ok=True)
subprocess.run([sys.executable, str(ROOT / "genpaths.py")], check=True, cwd=ROOT)
paths = json.loads((data / "paths.json").read_text(encoding="utf-8"))
inc = (data / "inbounds.inc").read_text(encoding="utf-8")
check("4 paths generated", len(paths) == 4, sorted(paths))
check("all paths unique", len(set(paths.values())) == 4)
check("all paths under /lumen/", all(v.startswith("/lumen/") for v in paths.values()))
check("inbounds.inc has 4 locations", inc.count("location =") == 4)

print("\n[2] bootstrap core config")
bs = load("bootstrap")
cfg = bs.core_config()
inbounds = cfg["inbounds"]

check("4 inbounds", len(inbounds) == 4, [i["tag"] for i in inbounds])
check("no trojan", {i["protocol"] for i in inbounds} == {"vless", "vmess"}, sorted({i["protocol"] for i in inbounds}))
check("ports are the 4 slots", sorted(i["port"] for i in inbounds) == [10001, 10002, 10004, 10005])
check("all listen on loopback", all(i["listen"] == "127.0.0.1" for i in inbounds))


def path_of(inb):
    stream = inb["streamSettings"]
    return stream.get("wsSettings", stream.get("httpupgradeSettings"))["path"]


check(
    "every inbound path matches paths.json",
    all(path_of(i) == paths[i["tag"]] for i in inbounds),
)

groups = {}
for row in bs.INBOUNDS:
    groups.setdefault(row[7], []).append(row[0])
check("pro group has 1 config", len(groups.get("pro", [])) == 1, groups.get("pro"))
check("std group has 3 configs", len(groups.get("std", [])) == 3, groups.get("std"))
check("10 templates", len(bs.TEMPLATES) == 10)
check(
    "every group tag appears in the core config",
    {tag for tags in groups.values() for tag in tags} == {i["tag"] for i in inbounds},
)

print("\n[3] proxy service")
px = load("proxy_service")
sample = json.dumps(
    [
        {"url": "socks5://1.2.3.4:1080", "country": "DE"},
        {"url": "http://user:pass@5.6.7.8:3128", "country": "US"},
        {"url": "ftp://bad:9999", "country": "XX"},
        {"url": "socks5://1.2.3.4:1080", "country": "DE"},
    ]
)
records = px.parse_catalog(sample)
check("duplicates collapsed and bad scheme dropped", len(records) == 2, len(records))
public = px.public_record(records[0])
check("no endpoint in the public record", "_endpoint" not in public and "url" not in public, sorted(public))
check("proxy id is stable", px.proxy_id("socks5://1.2.3.4:1080") == px.proxy_id("socks5://1.2.3.4:1080"))
check(
    "proxy id differs per endpoint",
    px.proxy_id("socks5://1.2.3.4:1080") != px.proxy_id("http://9.9.9.9:80"),
)

print("\n[4] no leftover branding")
for name in ("bootstrap.py", "genpaths.py", "proxy_service.py", "lumen-ui.js",
             "nginx.conf.template", "entrypoint.sh", "Dockerfile", "sub.html"):
    text = (ROOT / name).read_text(encoding="utf-8").lower()
    check(f"{name} is clean", "jinx" not in text)

print("\n[5] nginx template")
nginx = (ROOT / "nginx.conf.template").read_text(encoding="utf-8")
check("braces balanced", nginx.count("{") == nginx.count("}"))
check("inbounds.inc is included", "/var/lib/lumen/inbounds.inc" in nginx)
check("add-on script is injected", "/lumen-ui.js" in nginx and "sub_filter" in nginx)
check("owner service is routed", "lumenowner" in nginx)
check("proxy service is routed", "lumenproxy" in nginx)
check("proxy prefix beats /api", "^~ /api/lumen/proxy/" in nginx)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("All checks passed.")