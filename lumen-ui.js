/* Lumen dashboard add-on.

   The PasarGuard UI is kept as it is. This script only adds Lumen's own
   features on top of it:

   1) A "Proxy" page: the managed exit-proxy repository, per-country health,
      a manual refresh, a bulk re-test, and a preferred-proxy choice per
      country. Endpoint URLs and credentials never reach the browser.
   2) The "API Keys" page gets a section to issue the 5-minute owner key.
   3) The "Settings" page gets a change-password card.
   4) The login page gets an "Owner access" dialog for the owner key.

   Styling uses the Material 3 Expressive tokens, mapped onto whatever color
   format the panel's own theme uses, so the add-on follows the panel's light
   and dark themes instead of imposing a second theme on it. */
(function () {
  "use strict";

  var OWNER = null;
  var askedFor = null;
  var busy = false;

  /* ── Theme colour bridge ───────────────────────────────────────────────────
     The panel has used HSL triplet variables, then raw values, and now oklch.
     Read the format once and write CSS in the matching syntax. */
  var CFMT = null;
  function cv(name, fb) {
    if (CFMT === null) {
      var v = "";
      try {
        v = getComputedStyle(document.documentElement).getPropertyValue("--background").trim();
      } catch (e) {}
      CFMT = !v
        ? "none"
        : /^[\d.]+(deg)?\s+[\d.]+%\s+[\d.]+%/.test(v)
          ? "hsl"
          : "raw";
    }
    if (CFMT === "hsl") return "hsl(var(--" + name + "," + fb + "))";
    if (CFMT === "raw") return "var(--" + name + ",hsl(" + fb + "))";
    return "hsl(" + fb + ")";
  }

  function alpha(name, fb, pct) {
    if (CFMT === "hsl") {
      // color-mix needs a real colour, so fill the triplet from the fallback.
      return "color-mix(in srgb,hsl(var(--" + name + "," + fb + ")) " + pct + "%,transparent)";
    }
    return "color-mix(in srgb," + cv(name, fb) + " " + pct + "%,transparent)";
  }

  /* {{token|fallback}} or {{token|fallback|pct}} -> a themed CSS value. */
  function themed(css) {
    return css.replace(/\{\{([a-z-]+)\|([^}|]+)(?:\|(\d+))?\}\}/g, function (_, n, fb, p) {
      return p ? alpha(n, fb, p) : cv(n, fb);
    });
  }

  function addCss(id, css) {
    if (document.getElementById(id)) return;
    var style = document.createElement("style");
    style.id = id;
    style.textContent = themed(css);
    document.head.appendChild(style);
  }

  /* Set an element's inline style, resolving {{token|fallback}} placeholders
     through the same theme bridge the stylesheet uses. addCss() cannot help
     here: it only rewrites <style> text, not inline declarations. */
  function setStyle(node, css) {
    node.style.cssText = themed(css);
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ── Auth token ────────────────────────────────────────────────────────────
     The panel keeps its bearer token in localStorage under a name that has
     changed between releases, so probe the known keys. */
  function findToken() {
    var candidates = ["token", "access_token", "accessToken", "auth_token"];
    try {
      for (var i = 0; i < candidates.length; i++) {
        var raw = localStorage.getItem(candidates[i]);
        if (!raw) continue;
        try {
          var parsed = JSON.parse(raw);
          if (typeof parsed === "string") return parsed;
          if (parsed && parsed.access_token) return parsed.access_token;
          if (parsed && parsed.token) return parsed.token;
        } catch (e) {
          return raw;
        }
      }
    } catch (e) {}
    return "";
  }

  function api(path, options) {
    var opts = options || {};
    var headers = opts.headers || {};
    var token = findToken();
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(path, { method: opts.method || "GET", headers: headers, body: opts.body });
  }

  function post(path, payload, extraHeaders) {
    var headers = { "Content-Type": "application/json" };
    if (extraHeaders) {
      for (var k in extraHeaders) headers[k] = extraHeaders[k];
    }
    return api(path, { method: "POST", headers: headers, body: JSON.stringify(payload || {}) });
  }

  /* ── Owner detection ───────────────────────────────────────────────────────
     Asked once per token, never spammed. */
  function whoAmI() {
    var token = findToken();
    if (!token) {
      OWNER = null;
      askedFor = null;
      return Promise.resolve(null);
    }
    if (token === askedFor) return Promise.resolve(OWNER);
    if (busy) return Promise.resolve(OWNER);
    busy = true;
    return api("/api/admin")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (me) {
        busy = false;
        askedFor = token;
        // A rejected token means "unknown", not "reseller".
        OWNER = me ? !!(me.role && me.role.is_owner) : null;
        schedule();
        return OWNER;
      })
      .catch(function () {
        busy = false;
        return OWNER;
      });
  }

  function schedule() {
    if (window.__lumenTimer) clearTimeout(window.__lumenTimer);
    window.__lumenTimer = setTimeout(function () {
      if (findToken()) whoAmI();
    }, 4000);
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     1) The Proxy page
     ═════════════════════════════════════════════════════════════════════════ */

  var STATE = {
    proxies: [],
    status: null,
    preferred: {},
    assignments: {},
    users: [],
    loading: false,
    testing: false,
    lastError: ""
  };

  function countryKey(record) {
    return record.code || record.id;
  }

  /* Collapse the flat proxy list into one row per country. The user picks a
     country, not an endpoint: which concrete proxy serves that country is an
     implementation detail that must never be visible. */
  function byCountry() {
    var map = new Map();
    STATE.proxies.forEach(function (record) {
      var key = countryKey(record);
      var entry = map.get(key);
      if (!entry) {
        entry = {
          key: key,
          flag: record.flag,
          country: record.country,
          code: record.code,
          total: 0,
          healthy: 0,
          best: null
        };
        map.set(key, entry);
      }
      entry.total += 1;
      if (record.healthy) {
        entry.healthy += 1;
        // Prefer the healthiest, then the most-tested.
        if (
          !entry.best ||
          record.health_percent > entry.best.health_percent ||
          (record.health_percent === entry.best.health_percent &&
            (record.checked_at || "") > (entry.best.checked_at || ""))
        ) {
          entry.best = record;
        }
      }
    });
    return Array.from(map.values()).sort(function (a, b) {
      return a.country.localeCompare(b.country);
    });
  }

  function proxyPageCss() {
    return [
      ".lm-proxy-wrap{padding:20px;max-width:1180px;margin:0 auto}",
      ".lm-head{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:20px}",
      ".lm-title{font-size:1.6rem;font-weight:700;letter-spacing:-.02em;margin:0}",
      ".lm-sub{color:{{muted-foreground|0.7}};font-size:.86rem;margin:6px 0 0;max-width:60ch;line-height:1.6}",
      ".lm-actions{display:flex;gap:10px;flex-wrap:wrap}",
      ".lm-btn{min-height:44px;padding:0 20px;border-radius:9999px;border:1px solid transparent;",
      "font:600 .84rem inherit;cursor:pointer;display:inline-flex;align-items:center;gap:8px;",
      "transition:transform 180ms cubic-bezier(.2,0,0,1),opacity 180ms}",
      ".lm-btn:active{transform:scale(.97)}",
      ".lm-btn:disabled{opacity:.55;cursor:not-allowed}",
      ".lm-btn-primary{background:{{primary|0.6}};color:{{primary-foreground|0.98}};box-shadow:0 6px 18px {{primary|0.6}}/25}",
      ".lm-btn-tonal{background:{{card|0.35}};color:{{foreground|0.9}};border-color:{{border|0.85}}}",
      ".lm-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}",
      ".lm-stat{background:{{card|0.35}};border:1px solid {{border|0.85}};border-radius:16px;padding:16px 18px}",
      ".lm-stat-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:{{muted-foreground|0.7}}}",
      ".lm-stat-value{font-size:1.6rem;font-weight:750;letter-spacing:-.03em;margin-top:6px;display:block}",
      ".lm-bar{height:6px;border-radius:9999px;background:{{border|0.85}};overflow:hidden;margin-top:10px}",
      ".lm-bar>i{display:block;height:100%;background:{{primary|0.6}};border-radius:9999px;transition:width 400ms cubic-bezier(.2,0,0,1)}",
      ".lm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:14px}",
      ".lm-card{background:{{card|0.35}};border:1px solid {{border|0.85}};border-radius:16px;padding:16px 18px;",
      "display:flex;flex-direction:column;gap:10px}",
      ".lm-card-top{display:flex;align-items:center;gap:10px}",
      ".lm-flag{font-size:1.6rem;line-height:1}",
      ".lm-country{font-weight:650;font-size:.94rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".lm-pick{font-size:.7rem;font-weight:700;padding:3px 9px;border-radius:9999px;",
      "background:{{primary|0.6}};color:{{primary-foreground|0.98}}}",
      ".lm-meter{display:flex;align-items:center;gap:8px}",
      ".lm-meter .lm-bar{flex:1;margin:0}",
      ".lm-meter span{font-size:.74rem;color:{{muted-foreground|0.7}};font-variant-numeric:tabular-nums;min-width:34px;text-align:right}",
      ".lm-card-foot{display:flex;gap:8px;margin-top:auto}",
      ".lm-card-foot button{flex:0 0 auto}",
      ".lm-card-foot select{flex:1;min-width:0}",
      ".lm-empty{text-align:center;padding:48px 20px;color:{{muted-foreground|0.7}}}",
      ".lm-note{margin-top:18px;padding:14px 16px;border-radius:12px;background:{{card|0.35}};",
      "border:1px solid {{border|0.85}};font-size:.8rem;line-height:1.7;color:{{muted-foreground|0.7}}}",
      ".lm-note code{background:{{border|0.85}};padding:2px 6px;border-radius:6px;font-size:.95em}",
      ".lm-err{margin-bottom:16px;padding:12px 16px;border-radius:12px;background:{{destructive|0.55}}/12;",
      "border:1px solid {{destructive|0.55}}/30;color:{{destructive|0.55}};font-size:.84rem}",
      "@media(max-width:640px){.lm-proxy-wrap{padding:14px}.lm-head{flex-direction:column;align-items:stretch}.lm-actions{width:100%}.lm-btn{flex:1;justify-content:center}}",
      "@media(prefers-reduced-motion:reduce){.lm-btn,.lm-bar>i{transition:none}}"
    ].join("");
  }

  function renderProxyPage(root) {
    addCss("lm-proxy-css", proxyPageCss());
    root.innerHTML = "";

    var wrap = el("div", "lm-proxy-wrap");

    /* Header */
    var head = el("div", "lm-head");
    var headText = el("div");
    headText.appendChild(el("h1", "lm-title", "Exit proxies"));
    headText.appendChild(
      el(
        "p",
        "lm-sub",
        "Pick a country and Lumen routes that config's traffic through the fastest healthy proxy for it. Endpoint addresses stay on the server."
      )
    );
    head.appendChild(headText);

    var actions = el("div", "lm-actions");
    var refreshBtn = el("button", "lm-btn lm-btn-primary", "Refresh catalog");
    refreshBtn.onclick = function () {
      refreshCatalog(refreshBtn);
    };
    var testBtn = el("button", "lm-btn lm-btn-tonal", "Test all");
    testBtn.onclick = function () {
      testAll(testBtn);
    };
    actions.appendChild(refreshBtn);
    actions.appendChild(testBtn);
    head.appendChild(actions);
    wrap.appendChild(head);

    if (STATE.lastError) {
      wrap.appendChild(el("div", "lm-err", STATE.lastError));
    }

    /* Summary tiles */
    var stats = el("div", "lm-stats");
    var countries = byCountry();
    var healthy = STATE.proxies.filter(function (p) {
      return p.healthy;
    }).length;
    var usable = countries.filter(function (c) {
      return c.best;
    }).length;

    stats.appendChild(statTile("Configured", String(STATE.status ? STATE.status.catalog_size : STATE.proxies.length)));
    stats.appendChild(statTile("Healthy", String(healthy)));
    stats.appendChild(statTile("Countries", String(usable)));
    stats.appendChild(
      statTile(
        "Last refresh",
        STATE.status && STATE.status.last_refresh
          ? new Date(STATE.status.last_refresh * 1000).toLocaleTimeString()
          : "—"
      )
    );
    wrap.appendChild(stats);

    /* Country grid */
    if (!countries.length) {
      var empty = el("div", "lm-empty");
      empty.appendChild(el("p", null, STATE.loading ? "Loading the proxy catalog…" : "No proxies loaded yet."));
      if (!STATE.loading && STATE.status && !STATE.status.configured) {
        empty.appendChild(
          el(
            "p",
            null,
            "Set LUMEN_PROXY_REPO_URL together with LUMEN_S3_ACCESS_KEY_ID and LUMEN_S3_SECRET_ACCESS_KEY, then restart."
          )
        );
      }
      wrap.appendChild(empty);
    } else {
      var grid = el("div", "lm-grid");
      countries.forEach(function (entry) {
        grid.appendChild(countryCard(entry));
      });
      wrap.appendChild(grid);
    }

    var note = el("div", "lm-note");
    note.innerHTML =
      "A country choice applies to every config you assign below. " +
      "Endpoint addresses stay on the server; the user only ever sees a country. " +
      "Set <code>PROXY_REPOSITORY_MANUAL_REFRESH_KEY</code> to require a key on the write endpoints.";
    wrap.appendChild(note);

    root.appendChild(wrap);
    root.appendChild(renderAssignment());
  }

  /* Per-config country preference.

     The panel attaches a user to an inbound through its group, so a country is
     stored here as an intent and applied to the core config as a routing rule.
     It is a preference, not a guarantee: a country with no healthy proxy falls
     back to the direct route, and the card says so. */
  function renderAssignment() {
    var section = el("section", "");
    section.style.marginTop = "26px";
    section.appendChild(el("h2", "lm-title", "Preferred country per config"));
    section.appendChild(
      el(
        "p",
        "lm-sub",
        "Choose the exit country a config prefers. Its traffic leaves through that country's " +
        "fastest healthy proxy; if none is available the config uses the direct route."
      )
    );

    if (!STATE.users.length) {
      section.appendChild(el("p", "lm-sub", "No configs found in this panel yet."));
      return section;
    }

    var healthy = {};
    byCountry().forEach(function (entry) {
      if (entry.best) healthy[entry.code || entry.key] = entry;
    });

    var grid = el("div", "lm-grid");
    grid.style.marginTop = "14px";
    STATE.users.forEach(function (user) {
      grid.appendChild(assignmentCard(user, healthy));
    });
    section.appendChild(grid);
    return section;
  }

  function assignmentCard(user, healthy) {
    var card = el("div", "lm-card");
    var top = el("div", "lm-card-top");
    top.appendChild(el("span", "lm-country", user.username));
    card.appendChild(top);

    var current = STATE.assignments[user.username] || "";
    var select = el("select", "");
    setStyle(
      select,
      "min-height:44px;border-radius:12px;border:1px solid {{border|0.85}} 0;" +
        "background:{{background|0.98}} 0;color:{{foreground|0.9}} 0;padding:0 12px;font:inherit;width:100%"
    );
    var direct = el("option", "", "Direct (server IP)");
    direct.value = "";
    select.appendChild(direct);
    Object.keys(healthy).sort().forEach(function (code) {
      var entry = healthy[code];
      var option = el("option", "", entry.flag + "  " + entry.country);
      option.value = code;
      if (code === current) option.selected = true;
      select.appendChild(option);
    });
    // A saved country whose proxy is gone stays visible so the operator can see
    // why the config is not actually routed.
    if (current && !healthy[current]) {
      var stale = el("option", "", "⚠ " + current + " (no healthy proxy)");
      stale.value = current;
      stale.selected = true;
      select.appendChild(stale);
    }

    var apply = el("button", "lm-btn lm-btn-primary", "Save");
    apply.onclick = function () {
      assign(user.username, select.value, apply);
    };
    var foot = el("div", "lm-card-foot");
    foot.appendChild(select);
    foot.appendChild(apply);
    card.appendChild(foot);

    var note = el("div", "lm-meter");
    var span = el("span", null, "");
    if (current && healthy[current]) {
      span.textContent = "via " + healthy[current].flag + " " + healthy[current].country;
    } else if (current) {
      span.textContent = "direct — no healthy proxy for " + current;
    } else {
      span.textContent = "direct (server IP)";
    }
    note.appendChild(span);
    card.appendChild(note);
    return card;
  }

  function assign(username, country, button) {
    if (button) button.disabled = true;
    return post("/api/lumen/proxy/assign", { username: username, country: country }, writeHeaders())
      .then(function (r) {
        return r.json().then(function (body) {
          return { ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        if (button) button.disabled = false;
        STATE.assignments = (res.body && res.body.assignments) || STATE.assignments;
        STATE.lastError = res.ok ? "" : res.body.detail || "Could not save the assignment.";
        rerender();
      })
      .catch(function () {
        if (button) button.disabled = false;
        STATE.lastError = "Could not reach the proxy service.";
        rerender();
      });
  }

  function statTile(label, value) {
    var tile = el("div", "lm-stat");
    tile.appendChild(el("span", "lm-stat-label", label));
    tile.appendChild(el("strong", "lm-stat-value", value));
    return tile;
  }

  function countryCard(entry) {
    var card = el("div", "lm-card");

    var top = el("div", "lm-card-top");
    top.appendChild(el("span", "lm-flag", entry.flag || "🏳️"));
    top.appendChild(el("span", "lm-country", entry.country));
    var chosen = STATE.preferred[entry.code] === (entry.best && entry.best.id);
    if (chosen) top.appendChild(el("span", "lm-pick", "preferred"));
    card.appendChild(top);

    var meter = el("div", "lm-meter");
    var bar = el("div", "lm-bar");
    var fill = el("i");
    var percent = entry.best ? entry.best.health_percent : 0;
    fill.style.width = percent + "%";
    bar.appendChild(fill);
    meter.appendChild(bar);
    meter.appendChild(el("span", null, percent + "%"));
    card.appendChild(meter);

    var foot = el("div", "lm-card-foot");
    if (entry.best) {
      var use = el("button", "lm-btn lm-btn-primary", "Use for this country");
      use.onclick = function () {
        choosePreferred(entry.code, entry.best.id, use);
      };
      foot.appendChild(use);

      var retest = el("button", "lm-btn lm-btn-tonal", "Test");
      retest.onclick = function () {
        testOne(entry.best.id, retest);
      };
      foot.appendChild(retest);
    } else {
      var empty = el("button", "lm-btn lm-btn-tonal", "No healthy proxy");
      empty.disabled = true;
      foot.appendChild(empty);
    }
    card.appendChild(foot);

    return card;
  }

  function refreshKey() {
    return window.localStorage.getItem("lumen-proxy-key") || "";
  }

  function writeHeaders() {
    var key = refreshKey();
    return key ? { "X-Lumen-Key": key } : {};
  }

  function refreshCatalog(button) {
    if (button) button.disabled = true;
    STATE.loading = true;
    return post("/api/lumen/proxy/refresh", {}, writeHeaders())
      .then(function () {
        return loadCatalog();
      })
      .catch(function (err) {
        STATE.lastError = "Could not reach the proxy service.";
      })
      .then(function () {
        STATE.loading = false;
        if (button) button.disabled = false;
        rerender();
      });
  }

  function testAll(button) {
    if (button) button.disabled = true;
    STATE.testing = true;
    return post("/api/lumen/proxy/test-all", { limit: 60 }, writeHeaders())
      .catch(function () {
        STATE.lastError = "The test run did not finish.";
      })
      .then(function () {
        STATE.testing = false;
        if (button) button.disabled = false;
        return loadCatalog();
      })
      .then(rerender);
  }

  function testOne(id, button) {
    if (button) button.disabled = true;
    return post("/api/lumen/proxy/test", { id: id }, writeHeaders())
      .catch(function () {})
      .then(function () {
        if (button) button.disabled = false;
        return loadCatalog();
      })
      .then(rerender);
  }

  function choosePreferred(code, id, button) {
    if (button) button.disabled = true;
    return post("/api/lumen/proxy/preferred", { country: code, id: id }, writeHeaders())
      .then(function () {
        return loadPreferred();
      })
      .then(rerender);
  }

  function loadCatalog() {
    return fetch("/api/lumen/proxy/catalog")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        STATE.proxies = data.proxies || [];
        STATE.status = {
          catalog_size: data.count,
          last_refresh: data.last_refresh,
          configured: true,
          last_error: data.last_error
        };
        if (data.last_error) STATE.lastError = "Repository: " + data.last_error;
        else STATE.lastError = "";
      });
  }

  function loadPreferred() {
    return fetch("/api/lumen/proxy/preferred")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) STATE.preferred = data.preferred || {};
      });
  }

  function loadAssignments() {
    return fetch("/api/lumen/proxy/assignments")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) STATE.assignments = data.assignments || {};
      });
  }

  /* The panel's own users endpoint, so the assignment list matches what the
     operator sees in Users. */
  function loadUsers() {
    return api("/api/users?limit=200")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) return;
        var list = Array.isArray(data) ? data : data.users || [];
        STATE.users = list
          .filter(function (u) {
            return u && u.username;
          })
          .map(function (u) {
            return {
              username: u.username,
              status: u.status || "",
              used: u.used_traffic || 0
            };
          });
      })
      .catch(function () {
        STATE.users = [];
      });
  }

  function loadStatus() {
    return fetch("/api/lumen/proxy/status")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) STATE.status = data;
      });
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     2) Mounting the page inside the panel's router
     ═════════════════════════════════════════════════════════════════════════
     The panel is a SPA. Its route table is not public API, so the page is
     mounted by observing the location and re-rendering when it matches. */
  var MOUNT_ID = "lm-proxy-root";
  var observer = null;

  function routePath() {
    var path = location.pathname.replace(/^\/dashboard/, "");
    return path.replace(/\/+$/, "") || "/";
  }

  function isProxyRoute() {
    return routePath() === "/lumen/proxy";
  }

  function currentPage() {
    /* The panel renders into the outlet below the sidebar. Find it by looking
       for the main content container rather than guessing a class name. */
    var candidates = [
      document.querySelector("#root > div > div:nth-child(2)"),
      document.querySelector("#root > div:last-child"),
      document.querySelector("main"),
      document.querySelector("#root")
    ];
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i]) return candidates[i];
    }
    return document.getElementById("root") || document.body;
  }

  function mount() {
    if (!isProxyRoute()) {
      unmount();
      return;
    }
    var host = currentPage();
    if (!host) return;

    var root = document.getElementById(MOUNT_ID);
    if (!root) {
      root = el("div", "", "");
      root.id = MOUNT_ID;
      host.innerHTML = "";
      host.appendChild(root);
      loadStatus()
        .then(loadCatalog)
        .then(loadPreferred)
        .then(loadAssignments)
        .then(loadUsers)
        .then(rerender);
    } else {
      rerender();
    }
  }

  function unmount() {
    var root = document.getElementById(MOUNT_ID);
    if (root && root.parentNode) root.parentNode.removeChild(root);
  }

  function rerender() {
    var root = document.getElementById(MOUNT_ID);
    if (root) renderProxyPage(root);
  }

  /* ── Sidebar entry ──────────────────────────────────────────────────────────
     The panel's sidebar is rebuilt on navigation, so the item is re-asserted
     whenever the DOM changes. */
  function ensureNavItem() {
    if (document.querySelector('[data-lumen-proxy-nav]')) return;
    var links = Array.prototype.slice.call(document.querySelectorAll('a[href]'));
    var anchor = null;
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      if (/^\/dashboard\/(users|settings)/.test(href)) {
        anchor = links[i];
        break;
      }
    }
    if (!anchor || !anchor.parentNode) return;

    var item = el("a", null, "");
    item.href = "/dashboard/lumen/proxy";
    item.setAttribute("data-lumen-proxy-nav", "1");
    item.setAttribute("role", "link");
    // Reuse the panel's own item styling so it matches the rest of the menu.
    item.className = anchor.className;
    item.innerHTML =
      '<span style="display:inline-flex;align-items:center;gap:10px">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="10"></circle><path d="M2 12h20"></path>' +
      '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>' +
      "<span>Proxies</span></span>";
    anchor.parentNode.insertBefore(item, anchor);
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     3) Owner key, change password, owner access
     ═════════════════════════════════════════════════════════════════════════ */

  function dialogShell(title, bodyNodes, actions) {
    var backdrop = el("div", "");
    setStyle(
      backdrop,
      "position:fixed;inset:0;z-index:9999;display:grid;place-items:center;" +
        "padding:20px;background:{{background|0.1}}/70;backdrop-filter:blur(6px)"
    );
    var box = el("div", "");
    setStyle(
      box,
      "width:min(460px,100%);border-radius:28px;padding:28px;" +
        "background:{{card|0.98}};color:{{foreground|0.9}};border:1px solid {{border|0.85}};" +
        "box-shadow:0 24px 70px rgba(0,0,0,.28);display:flex;flex-direction:column;gap:16px"
    );
    var heading = el("h2", "", title);
    setStyle(heading, "margin:0;font-size:1.2rem;font-weight:700");
    box.appendChild(heading);
    bodyNodes.forEach(function (n) {
      if (n) box.appendChild(n);
    });
    var row = el("div", "");
    setStyle(row, "display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap");
    actions.forEach(function (a) {
      row.appendChild(a);
    });
    box.appendChild(row);
    backdrop.appendChild(box);
    backdrop.addEventListener("click", function (e) {
      if (e.target === backdrop) backdrop.remove();
    });
    document.body.appendChild(backdrop);
    return { backdrop: backdrop, box: box, row: row };
  }

  function field(labelText, type) {
    var wrap = el("label", "");
    setStyle(wrap, "display:flex;flex-direction:column;gap:6px;font-size:.82rem");
    wrap.appendChild(el("span", "", labelText));
    var input = el("input", "");
    input.type = type || "text";
    input.autocomplete = "off";
    setStyle(
      input,
      "min-height:46px;padding:0 14px;border-radius:12px;border:1px solid {{border|0.85}};" +
        "background:{{background|0.98}};color:{{foreground|0.9}};font:inherit"
    );
    wrap.appendChild(input);
    return { wrap: wrap, input: input };
  }

  function button(text, primary) {
    var b = el("button", "", text);
    setStyle(
      b,
      "min-height:44px;padding:0 20px;border-radius:9999px;border:1px solid transparent;cursor:pointer;" +
        "font:600 .84rem inherit;" +
        (primary
          ? "background:{{primary|0.6}};color:{{primary-foreground|0.98}}"
          : "background:transparent;color:{{muted-foreground|0.7}}")
    );
    return b;
  }

  function openOwnerKey() {
    var status = el("div", "");
    setStyle(status, "font-size:.82rem;line-height:1.6;color:{{muted-foreground|0.7}}");
    var cancel = button("Close", false);
    cancel.onclick = function () {
      dialog.backdrop.remove();
    };
    var dialog = dialogShell("5-minute owner key", [status], [cancel]);

    whoAmI().then(function (owner) {
      if (owner === false) {
        status.textContent = "Only the panel owner can issue this key.";
        return;
      }
      status.textContent = "Checking…";
      post("/lumen/key", {})
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          if (res.ok && res.body.key) {
            status.textContent =
              "Key: " + res.body.key + " — valid for " + (res.body.ttl || 300) + " seconds, single use.";
          } else {
            status.textContent = res.body.detail || "Could not issue a key.";
          }
        })
        .catch(function () {
          status.textContent = "Could not reach the server.";
        });
    });
  }

  function openOwnerAccess() {
    var key = field("Owner key", "text");
    var user = field("New username", "text");
    var pass = field("New password", "password");
    var status = el("div", "");
    setStyle(status, "font-size:.82rem;color:{{muted-foreground|0.7}};min-height:1.2em");

    var actions = [];
    var dialog = null;
    var cancel = button("Cancel", false);
    cancel.onclick = function () {
      dialog.backdrop.remove();
    };
    var go = button("Save new details", true);
    go.onclick = function () {
      status.textContent = "";
      go.disabled = true;
      post("/lumen/reset", {
        key: key.input.value,
        username: user.input.value,
        password: pass.input.value
      })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          go.disabled = false;
          if (res.ok) {
            status.textContent =
              "Saved. Sign in with “" + res.body.username + "” and the new password.";
          } else {
            status.textContent = res.body.detail || "Not saved, try again.";
          }
        })
        .catch(function () {
          go.disabled = false;
          status.textContent = "Could not reach the server.";
        });
    };
    actions.push(cancel, go);

    dialog = dialogShell("Owner access", [status, key.wrap, user.wrap, pass.wrap], actions);
    user.input.focus();
  }

  function addPasswordCard() {
    if (document.getElementById("lm-pw-card")) return;
    var host = currentPage();
    if (!host) return;
    var card = el("div", "");
    card.id = "lm-pw-card";
    setStyle(
      card,
      "margin:16px;padding:20px;border-radius:16px;border:1px solid {{border|0.85}};" +
        "background:{{card|0.35}};color:{{foreground|0.9}}"
    );

    var user = field("Current username", "text");
    var cur = field("Current password", "password");
    var next = field("New password", "password");
    var nextUser = field("New username (optional)", "text");
    var status = el("div", "");
    setStyle(status, "font-size:.82rem;color:{{muted-foreground|0.7}};min-height:1.2em");
    var save = button("Save new password", true);
    save.onclick = function () {
      status.textContent = "";
      save.disabled = true;
      post("/lumen/password", {
        username: user.input.value,
        current: cur.input.value,
        new: next.input.value,
        new_username: nextUser.input.value || null
      })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          save.disabled = false;
          status.textContent = res.ok
            ? "Saved. Sign in again in a moment."
            : res.body.detail || "Not saved, try again.";
        })
        .catch(function () {
          save.disabled = false;
          status.textContent = "Could not reach the server.";
        });
    };

    var heading = el("h3", "", "Change password");
    setStyle(heading, "margin:0 0 4px;font-size:1rem;font-weight:700");
    card.appendChild(heading);

    var blurb = el("p", "", "Verify your current password, then choose a new one.");
    setStyle(blurb, "margin:0 0 14px;font-size:.82rem;color:{{muted-foreground|0.7}}");
    card.appendChild(blurb);

    [user.wrap, cur.wrap, next.wrap, nextUser.wrap, status, save].forEach(function (n) {
      card.appendChild(n);
    });
    host.insertBefore(card, host.firstChild);
  }

  /* ── Login page ─────────────────────────────────────────────────────────────
     The owner dialog is opened by a button appended to the login form. */
  function addOwnerAccessButton() {
    var form = document.querySelector("form");
    if (!form || document.getElementById("lm-owner-btn")) return;
    if (!document.body.textContent.match(/sign in|login|ورود|вход|登录/i)) return;
    var ownerBtn = button("Owner access", false);
    ownerBtn.id = "lm-owner-btn";
    ownerBtn.style.width = "100%";
    ownerBtn.style.justifyContent = "center";
    ownerBtn.style.marginTop = "12px";
    ownerBtn.onclick = openOwnerAccess;
    form.appendChild(ownerBtn);
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     4) Boot
     ═════════════════════════════════════════════════════════════════════════ */

  function tick() {
    ensureNavItem();
    if (isProxyRoute()) {
      mount();
    } else {
      unmount();
      if (routePath() === "/settings" || routePath().indexOf("/api-keys") === 0) {
        addPasswordCard();
      }
    }
    addOwnerAccessButton();
    whoAmI();
  }

  function start() {
    addCss("lm-base-css", [
      ".lm-spin{display:inline-block;width:16px;height:16px;border-radius:50%;",
      "border:2px solid currentColor;border-right-color:transparent;animation:lm-rot .8s linear infinite}",
      "@keyframes lm-rot{to{transform:rotate(360deg)}}"
    ].join(""));

    if (window.MutationObserver) {
      observer = new MutationObserver(function () {
        if (window.__lumenQueued) return;
        window.__lumenQueued = true;
        requestAnimationFrame(function () {
          window.__lumenQueued = false;
          tick();
        });
      });
      observer.observe(document.body, { childList: true, subtree: true });
    }

    tick();
    setInterval(tick, 2000);
    window.addEventListener("popstate", tick);
    window.addEventListener("hashchange", tick);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();