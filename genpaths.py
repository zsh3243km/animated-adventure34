"""Lumen: per-install secret paths.

Every deploy gets its own random config paths on first boot (saved on the
volume), so no two panels share the same paths. Old installs keep their paths
so existing configs never break.
"""
import json, os, secrets, uuid

DATA = os.getenv("LUMEN_DATA", "/var/lib/lumen")
OUT_JSON = f"{DATA}/paths.json"
OUT_INC = f"{DATA}/inbounds.inc"

# tag -> (local port, path prefix)
SLOTS = {
    "LM-VLESS-WS-1": (10001, "/lumen/ws/"),
    "LM-VLESS-WS-2": (10002, "/lumen/stream/"),
    "LM-VMESS-WS":   (10004, "/lumen/vmess/"),
    "LM-VLESS-HU":   (10005, "/lumen/hu/"),
}

# Paths used by earlier Lumen builds: kept so an existing panel does not break
# when it is upgraded.
LEGACY = {
    "LM-VLESS-WS-1": "/lumen/ws/7a5a21d9-60f9-4542-943e-7838b90169e1",
    "LM-VLESS-WS-2": "/lumen/stream/4868e537-9fd8-46e9-b63a-f36459d18a81",
    "LM-VMESS-WS":   "/lumen/vmess/3d801b12-a333-452c-b9d5-90ece1a8d68c",
    "LM-VLESS-HU":   "/lumen/hu/cc046fa3-78ec-4619-ac8f-9d6c7a5d0755",
}


def load():
    try:
        with open(OUT_JSON) as f:
            paths = json.load(f)
        if all(isinstance(paths.get(tag), str) and paths[tag].startswith("/") for tag in SLOTS):
            return paths
    except Exception:
        pass
    return None


def main():
    os.makedirs(DATA, exist_ok=True)
    paths = load()
    if paths is None:
        if os.path.exists(f"{DATA}/.owner_initialized") or os.path.exists(f"{DATA}/db.sqlite3"):
            paths = dict(LEGACY)
            print("[paths] existing panel detected, keeping old config paths")
        else:
            paths = {
                tag: prefix + str(uuid.UUID(bytes=secrets.token_bytes(16), version=4))
                for tag, (_, prefix) in SLOTS.items()
            }
            print("[paths] new install: unique config paths generated")
        tmp = OUT_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(paths, f, indent=1)
        os.replace(tmp, OUT_JSON)

    lines = [
        f"location = {paths[tag]} {{ proxy_pass http://127.0.0.1:{port}; include /etc/nginx/ws.inc; }}"
        for tag, (port, _) in SLOTS.items()
    ]
    with open(OUT_INC, "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()