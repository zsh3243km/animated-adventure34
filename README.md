# Lumen v34

A one-service VPN panel: the [PasarGuard](https://github.com/PasarGuard/panel)
dashboard, an Xray core data plane, and nginx as the sole public TLS edge —
deployed as a single Railway service.

**What Lumen adds on top of the upstream panel:**

- **Exit proxies.** A managed proxy repository, per-country health, and a
  preferred-proxy choice per country. Routing is fail-closed: a config that
  selects a proxy only ever exits through that proxy.
- **Four ready configs.** VLESS + WebSocket (Pro), VLESS + WebSocket, VMess +
  WebSocket, VLESS + HTTPUpgrade — all with early data, TLS 443, `alpn=http/1.1`.
- **Sales tooling.** Two groups (Pro / Lumen), ten templates, a Reseller role,
  and a demo reseller account.
- **Self-healing.** A doctor loop repairs the core, node, groups, hosts,
  templates and subscription settings. A subscription guard checks every 3
  seconds and restores the subscription page within seconds.
- **Owner access.** A 5-minute single-use owner key, and password changes from
  inside the panel.

The panel's own UI is kept as it is. Lumen injects its add-on with a single
`<script>` tag, so there is no forked frontend to maintain.

---

## Deploy

1. **Fork** this repository.
2. **New Project → Deploy from GitHub repo** in Railway.
3. **Attach a Volume** at exactly:

   ```text
   /var/lib/lumen
   ```

   Without a volume, every restart loses users, configs and passwords.
4. **Settings → Networking → Generate Domain**, target port `8080`.
5. **Settings → Deploy → Region**: `EU West (Amsterdam)` for the lowest latency
   to Iran.
6. **Redeploy.**

The log line that means everything is ready:

```text
[bootstrap] READY -> https://YOUR-DOMAIN/dashboard/
```

### First login

| Role | URL | Username | Password |
|---|---|---|---|
| Owner | `https://YOUR-DOMAIN/dashboard/` | `admin` | `admin` |
| Demo reseller (50 GB) | `https://YOUR-DOMAIN/dashboard/` | `reseller` | `reseller` |

> `admin` / `admin` is for the **first** login only. Change it immediately in
> *Settings → Change password*.

### Forgotten password

1. Go to *API Keys* and press **Get key**, or read the `OWNER KEY` line in the
   Railway logs.
2. Sign out and press **Owner access** on the login page.
3. Paste the key, choose a new username and password.

Restarting the service in Railway prints a fresh key to the logs.

---

## Configs

| Group | Name | Protocol | Transport | Fingerprint |
|---|---|---|---|---|
| **Lumen Pro** | Lumen | VLESS | WebSocket + early data | chrome |
| **Lumen** | Stream | VLESS | WebSocket | firefox |
| | Diamond | VMess | WebSocket | edge |
| | Night | VLESS | HTTPUpgrade | ios |

Every config reaches Railway's TLS edge on 443 with `alpn=http/1.1`. Early data
(`?ed=2560`) rides the handshake, saving one round trip per connection.

**Supported apps:** v2rayNG, V2Box, Hiddify, Streisand, Happ, NekoBox, Clash
Meta, sing-box.

Config paths are random per install and stored on the volume, so no two Lumen
deployments share paths and an upgrade never breaks an existing config.

---

## Exit proxies (the Lumen addition)

The panel gets a **Proxies** page listing every country in the repository, its
health, and a single button to route that country through its fastest healthy
proxy. Below that, each config can be assigned a country.

**How routing works.** Choosing a country adds a reserved Xray user to that
config's client list and writes a routing rule keyed on it, so Xray sends only
that config's traffic through the proxy. The panel owns inbound clients and the
proxy service owns outbounds; the two are merged by the setup loop, which
rewrites the core whenever a country changes. Changes take effect within about
five minutes, or immediately if you restart the service.

**Privacy:** endpoint URLs and credentials never reach the browser. The page
shows a country flag, a name, a health percentage, and a stable non-reversible
ID. The user picks a country; which concrete proxy serves it is an internal
detail.

**Fail-closed:** a config assigned to a country routes through that country and
nothing else. If its proxy is unavailable the config has no rule at all and goes
direct, rather than silently reaching the internet through the server's own IP.
Assigning a country is therefore an explicit choice, not a default.

### Setup

Add these as Railway variables (see `env.example`), then redeploy:

```env
LUMEN_PROXY_REPO_URL=https://your-bucket.s3.amazonaws.com/proxies.json
LUMEN_S3_ACCESS_KEY_ID=...
LUMEN_S3_SECRET_ACCESS_KEY=...
PROXY_REPOSITORY_MANUAL_REFRESH_KEY=<a random string>
```

The repository may be a JSON array of proxy objects or plain strings:

```json
[
  { "url": "socks5://user:pass@de1.example.net:1080", "country": "DE" },
  { "url": "http://us1.example.net:8080", "country": "United States" }
]
```

`http`, `https`, `socks5` and `socks5h` are accepted; anything else is ignored.
`country` may be a code or a name. Without these variables the panel works
normally and the Proxies page explains what is missing.

To require a key on the write endpoints (refresh, test, assign), store it in the
dashboard's browser console once:

```js
localStorage.setItem('lumen-proxy-key', '<the same value>')
```

A failing repository fetch keeps the last good list instead of emptying the
catalog, so the dashboard stays usable during an outage.

---

## Environment variables

Everything is optional; the panel configures itself. See `env.example` for the
full annotated list. The most useful ones:

| Variable | Default | Purpose |
|---|---|---|
| `CONFIG_TITLE` | `Lumen` | Text shown after each config name |
| `DEMO_RESELLER` | `on` | Set to `off` to skip the demo reseller |
| `LUMEN_XRAY_LOGLEVEL` | `warning` | Xray log verbosity |
| `LUMEN_PROXY_REFRESH_SECONDS` | `7200` | Repository refetch interval; `0` disables |

---

## Layout

| File | Role |
|---|---|
| `Dockerfile` | Builds the image; pins and fetches the Xray core release |
| `entrypoint.sh` | Starts the node, the panel, the proxy service and nginx, each restarted within 1 second if it dies; a watchdog restarts the whole service if nothing answers for 3 minutes |
| `bootstrap.py` | Zero-touch setup, then the doctor and subscription guard loops |
| `proxy_service.py` | Proxy repository, probing, the routing plan, and the loopback API |
| `countries.py` | ISO 3166-1 codes, names and flag emoji, used to normalise repository metadata |
| `genpaths.py` | Generates the per-install secret config paths |
| `lumen-ui.js` | The dashboard add-on: Proxies page, owner key, change password |
| `nginx.conf.template` | The public edge, config paths, sub_filter injection |
| `ws.inc` | WebSocket settings shared by every config path |
| `sub.html` | The customer-facing subscription page |
| `healthcheck.sh` | Panel + Xray liveness, used by Docker and the watchdog |
| `verify_build.py` | Offline checks: paths, core config, catalog, nginx wiring |
| `verify_routing.py` | Offline checks: routing plan, outbound protocols, reserved user |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Application failed to respond` | The domain target port must be `8080` |
| Configs do not connect | The domain was created after the first deploy: redeploy once |
| Users vanish after a deploy | The volume is not attached at `/var/lib/lumen` |
| `Incorrect username or password` | Wait a minute for the service to finish starting |
| The Proxies page says it is not configured | The `LUMEN_PROXY_*` variables are not set |
| Boot fails on `paths.json` | `/code/genpaths.py` did not run; check the volume is writable |

---

## Credits

Built on [PasarGuard Panel](https://github.com/PasarGuard/panel),
[PasarGuard Node](https://github.com/PasarGuard/node), and
[Xray-core](https://github.com/XTLS/Xray-core). Each is licensed under its own
repository's terms.