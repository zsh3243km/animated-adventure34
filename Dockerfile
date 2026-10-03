# Lumen Panel v34
#
# One Railway service runs the PasarGuard panel, an Xray core data plane, and
# nginx as the sole public TLS edge. The panel's own UI is kept as-is; Lumen
# adds proxy support and a Material 3 Expressive skin through an injected
# add-on script rather than by forking the panel frontend.
FROM pasarguard/panel:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        openssl \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

# Xray core is pinned so a redeploy cannot silently change the data plane.
# geoip.dat / geosite.dat come from the same release so `geoip:private` resolves.
ARG XRAY_VERSION=v25.8.3

RUN mkdir -p /usr/local/share/xray \
    && curl -fsSL -o /tmp/xray.zip \
        "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" \
    && (unzip -q /tmp/xray.zip -d /usr/local/share/xray || \
        python3 -c "import zipfile;zipfile.ZipFile('/tmp/xray.zip').extractall('/usr/local/share/xray')") \
    && rm -f /tmp/xray.zip \
    && install -m 0755 /usr/local/share/xray/xray /usr/local/bin/xray \
    && rm -f /usr/local/share/xray/xray \
    && /usr/local/bin/xray version

COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY ws.inc /etc/nginx/ws.inc
COPY lumen-ui.js /etc/nginx/lumen-ui.js
COPY entrypoint.sh /entrypoint.sh
COPY bootstrap.py /code/bootstrap.py
COPY genpaths.py /code/genpaths.py
COPY proxy_service.py /code/proxy_service.py
COPY countries.py /code/countries.py
COPY sub.html /code/custom_templates/subscription/index.html
COPY sub.html /etc/lumen/sub.html
COPY healthcheck.sh /usr/local/bin/lumen-healthcheck

# Strip Windows line endings (safe if a file was edited on a PC), then make
# everything executable.
RUN sed -i "s/\r$//" /entrypoint.sh /usr/local/bin/lumen-healthcheck /code/bootstrap.py \
        /code/genpaths.py /code/proxy_service.py /code/countries.py \
        /etc/nginx/nginx.conf.template /etc/nginx/ws.inc /etc/nginx/lumen-ui.js \
    && chmod +x /entrypoint.sh /usr/local/bin/xray /usr/local/bin/lumen-healthcheck

ENV PORT=8080 \
    UVICORN_HOST=127.0.0.1 \
    UVICORN_PORT=8000 \
    UVICORN_PROXY_HEADERS=True \
    UVICORN_FORWARDED_ALLOW_IPS=127.0.0.1 \
    SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:////var/lib/lumen/db.sqlite3 \
    CUSTOM_TEMPLATES_DIRECTORY=/code/custom_templates/ \
    SUBSCRIPTION_PAGE_TEMPLATE=subscription/index.html \
    SUBSCRIPTION_PATH=sub \
    XRAY_EXECUTABLE_PATH=/usr/local/bin/xray \
    XRAY_ASSETS_PATH=/usr/local/share/xray \
    LUMEN_DATA=/var/lib/lumen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD lumen-healthcheck || exit 1

ENTRYPOINT ["/entrypoint.sh"]