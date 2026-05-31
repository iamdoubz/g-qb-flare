# bt4gprx.com — qBittorrent Search Plugin

A qBittorrent search-engine plugin for [bt4gprx.com](https://bt4gprx.com), a DHT
torrent index. It is written for **Python 3.12** and is designed to get past the
site's **Cloudflare** protection by "acting like a human": it reuses a single
session (so the `cf_clearance` cookie persists), sends a full browser header set,
spaces requests out with randomized delays, and can drive a real headless browser
(FlareSolverr) to clear JavaScript/Turnstile challenges.

Features:

- Free-text search across the whole site.
- **Category-focused search** — qBittorrent's categories are mapped onto bt4g's
  buckets (movies, tv, anime, music, books, software, games, pictures, all).
- **Cloudflare-resistant fetching** with three transports, tried in order:
  FlareSolverr → `curl_cffi` (TLS impersonation) → stdlib `urllib`.
- **Auto-updating trackers** — magnets are populated from
  [ngosang/trackerslist](https://github.com/ngosang/trackerslist)
  (`trackers_best.txt`), refreshed live at runtime with a baked-in offline fallback.

> **Note on the official repo.** Official qBittorrent search plugins are expected
> to use only the Python standard library. Because bt4gprx.com is behind
> Cloudflare, this plugin relies on extra tooling (FlareSolverr and/or
> `curl_cffi`), so it is **not** eligible for the official plugin repository.

---

## Table of contents

1. [Requirements](#requirements)
2. [How Cloudflare bypass works](#how-cloudflare-bypass-works)
3. [Docker Compose setup](#docker-compose-setup)
4. [Installing the plugin](#installing-the-plugin)
5. [Using the plugin](#using-the-plugin)
6. [Environment variables](#environment-variables)
7. [Category mapping](#category-mapping)
8. [Troubleshooting](#troubleshooting)

---

## Requirements

- qBittorrent with the **Search** tab enabled and a working Python interpreter.
- One of the following for Cloudflare (the plugin still loads without them, but
  will be blocked while Cloudflare is active):
  - **FlareSolverr** running as a reachable service *(recommended for Docker)*, **or**
  - the **`curl_cffi`** Python package installed in the *same* Python that
    qBittorrent uses for search.

The Docker Compose setup below uses the FlareSolverr route, which means the
qBittorrent container needs **no third-party Python packages at all**.

---

## How Cloudflare bypass works

When the plugin fetches a page it tries these transports in order and uses the
first that returns usable HTML:

1. **FlareSolverr** — only if `BT4G_FLARESOLVERR` is set. Runs a real headless
   browser, so it can clear actual JS / Turnstile challenges. Most human-like,
   heaviest. Uses only the standard library to talk to FlareSolverr.
2. **`curl_cffi`** — TLS / JA3 browser impersonation. Lightweight and fast, and
   clears Cloudflare when detection is fingerprint-based (the common case). This
   is the default when FlareSolverr is not configured.
3. **`urllib`** (stdlib) — fallback so the plugin still works if Cloudflare is
   off. Usually blocked while Cloudflare is active.

The tracker-list refresh always uses `curl_cffi` if present, otherwise `urllib`
(GitHub's raw host is not behind Cloudflare).

---

## Docker Compose setup

This example runs qBittorrent behind a **Gluetun** VPN and adds **FlareSolverr**
in the *same network namespace* so the two can talk over `localhost`.

> **Why the same namespace?** qBittorrent uses
> `network_mode: "service:gluetun"`, so it shares Gluetun's network stack. Placing
> FlareSolverr in that same namespace lets qBittorrent reach it at
> `http://localhost:8191/v1` with no extra firewall rules, and FlareSolverr's
> traffic also egresses through the VPN. If you instead put FlareSolverr on a
> normal bridge network, Gluetun's killswitch will block qBittorrent from reaching
> it unless you add your Docker subnet to `FIREWALL_OUTBOUND_SUBNETS`.

### `docker-compose.yml`

```yaml
services:
  gluetun:
    image: ghcr.io/qdm12/gluetun:latest
    mem_limit: 256M
    container_name: gluetun
    restart: unless-stopped
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    sysctls:
      - net.ipv6.conf.all.disable_ipv6=1
    environment:
      - VPN_SERVICE_PROVIDER=protonvpn
      - VPN_TYPE=wireguard
      - WIREGUARD_PRIVATE_KEY=${WIREGUARD_PRIVATE_KEY}
      - SERVER_COUNTRIES=${SERVER_COUNTRIES}
      - SERVER_CITIES=${SERVER_CITIES}
      - VPN_PORT_FORWARDING=on
      - TZ=${TZ}
      - QBT_WEBUI_ENABLED=true
    volumes:
      - ${CONFIG}/gluetun:/gluetun
      - ${CONFIG}/gluetun/auth/config.toml:/gluetun/auth/config.toml:ro
    ports:
      - "8080:8080"          # qBittorrent Web UI
      # - "8191:8191"        # (optional) expose FlareSolverr to the host for debugging
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://google.com"]
      interval: 30s
      timeout: 10s
      retries: 2

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    mem_limit: 4G
    restart: unless-stopped
    network_mode: "service:gluetun"     # all traffic through the VPN
    depends_on:
      gluetun:
        condition: service_healthy
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - WEBUI_PORT=8080
      - QBITTORRENT_INTERFACE=tun0
      - DOCKER_MODS=ghcr.io/t-anc/gsp-qbittorent-gluetun-sync-port-mod:main
      - GSP_GTN_API_KEY=${GSP_GTN_API_KEY:-randomapikey}
      - GSP_QBITTORRENT_PORT=${GSP_QBITTORRENT_PORT:-53764}
      - GSP_MINIMAL_LOGS=false
      - BT4G_FLARESOLVERR=http://localhost:8191/v1   # bt4gprx plugin → FlareSolverr
    volumes:
      - ${CONFIG}/config:/config
      - ${CONFIG}/incomplete:/incomplete
      - ${CONFIG}/DONE:/downloads
    ulimits:
      nofile:
        soft: 32768
        hard: 65536

  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    mem_limit: 1G
    shm_size: 1gb                        # headless Chrome needs more than the 64MB default
    restart: unless-stopped
    network_mode: "service:gluetun"      # same namespace → reachable at localhost:8191
    depends_on:
      gluetun:
        condition: service_healthy
    environment:
      - LOG_LEVEL=info
      - TZ=${TZ}
```

### `.env`

The compose file expects these variables (adjust to your setup):

```dotenv
CONFIG=/path/to/your/config
PUID=1000
PGID=1000
TZ=Etc/UTC
WIREGUARD_PRIVATE_KEY=your_wireguard_private_key
SERVER_COUNTRIES=Netherlands
SERVER_CITIES=
GSP_GTN_API_KEY=randomapikey
GSP_QBITTORRENT_PORT=53764
```

### Start it

```bash
docker compose up -d
```

Gluetun comes up first; qBittorrent and FlareSolverr start once the VPN is
healthy. The qBittorrent Web UI is at `http://<host>:8080`.

> **Important:** `BT4G_FLARESOLVERR` must be `http://localhost:8191/v1` here —
> **not** `http://flaresolverr:8191`. Because the containers share Gluetun's
> network stack, the service name will not resolve from inside the namespace, but
> `localhost` points at the shared stack.

---

## Installing the plugin

The plugin file (`plugins/bt4gprx/bt4gprx.py`) must be installed into qBittorrent. The cleanest
way is through the Web UI, which copies it into the correct engines directory and
persists it in your `/config` volume:

1. Open the qBittorrent Web UI (`http://<host>:8080`).
2. Go to the **Search** tab. If it is missing, enable it under
   **View → Search Engine**, then confirm the image detected Python.
3. Click **Search plugins…** (bottom right) → **Install a new one**.
4. Choose **Web link** and paste the raw URL to `bt4gprx.py`, **or** choose
   **Local file** and point to a path reachable inside the container.
5. Confirm `bt4gprx` appears in the plugin list and is enabled.

To update later, use **Check for updates** or **Uninstall** then reinstall.

### Alternative: install by file path

If you prefer to drop the file in manually, copy it into the search engines
directory inside the container's config volume (do **not** hand-place it unless
the Web UI method is unavailable — the exact path can vary by version):

```bash
docker cp plugins/bt4gprx/bt4gprx.py qbittorrent:/config/qBittorrent/nova3/engines/bt4gprx.py
# then restart qBittorrent so it picks up the new engine
docker restart qbittorrent
```

---

## Using the plugin

1. Open the **Search** tab in the qBittorrent Web UI.
2. Type your search terms.
3. Pick a **category** from the dropdown to focus the search, or leave it on
   **All categories**.
4. Run the search. Results are sorted with the most seeders first.
5. Double-click a result (or use the download button) to add it. Magnets are
   built with an up-to-date public tracker list.

### Testing outside qBittorrent

You can run the bundled `nova2.py` harness from within the container to verify the
engine works before relying on it in the UI:

```bash
python3 nova2.py bt4gprx all ubuntu
python3 nova2.py bt4gprx movies "big buck bunny"
```

Each result is one `|`-separated line:
`link | name | size | seeds | leech | engine_url | desc_link | pub_date`.

---

## Environment variables

All variables are **optional** and read by the plugin at runtime. Set them on the
container that runs qBittorrent's search (in the Compose example, the
`qbittorrent` service).

| Variable | Default | Description |
|---|---|---|
| `BT4G_FLARESOLVERR` | *(empty)* | FlareSolverr endpoint, e.g. `http://localhost:8191/v1`. When set, the plugin prefers FlareSolverr (real headless browser) to clear Cloudflare. Leave empty to skip FlareSolverr. |
| `BT4G_IMPERSONATE` | `chrome` | `curl_cffi` impersonation target (e.g. `chrome`, `chrome124`, `safari17`). Controls the browser TLS/JA3 fingerprint used by the lightweight transport. |
| `BT4G_UPDATE_TRACKERS` | `1` | Refresh the tracker list from ngosang/trackerslist at runtime. Set to `0` (also accepts `false`/`no`) to use only the embedded snapshot baked into the plugin. |
| `BT4G_MAX_PAGES` | `3` | Maximum number of result pages to walk per search. Higher = more results but more requests. Minimum effective value is `1`. |
| `BT4G_MIN_DELAY` | `1.5` | Minimum seconds to wait between requests (lower bound of the randomized human-like delay). |
| `BT4G_MAX_DELAY` | `4.0` | Maximum seconds to wait between requests (upper bound of the randomized human-like delay). |
| `BT4G_BASE_URL` | `https://bt4gprx.com` | Override the site base URL if the domain changes or you use a mirror. |

### Notes on values

- **`BT4G_MIN_DELAY` / `BT4G_MAX_DELAY`** are the randomized inter-request pause.
  Keeping a spread (rather than a fixed interval) is part of the human-like
  behavior; do not set them to `0` unless you are testing.
- **`BT4G_MAX_PAGES`** is clamped to at least `1`. Bad numeric values fall back to
  the default.
- **`BT4G_FLARESOLVERR`** must point at the `/v1` endpoint of a reachable
  FlareSolverr instance. In the VPN/namespace setup above this is
  `http://localhost:8191/v1`.

---

## Category mapping

qBittorrent has a fixed set of categories. bt4g groups content more coarsely, so
several qBittorrent categories map onto the same bt4g bucket:

| qBittorrent category | bt4g bucket |
|---|---|
| All | *(everything)* |
| Movies | movie |
| TV | movie *(bt4g has no separate TV bucket)* |
| Anime | movie |
| Music | audio |
| Books | doc |
| Software | app |
| Games | app |
| Pictures | *(everything — no dedicated image bucket)* |

If bt4g adds or changes categories, edit the `supported_categories` dictionary in
`bt4gprx.py`.

---

## Troubleshooting

**No results at all / every search is empty.**
Cloudflare is probably blocking the fetch. Make sure `BT4G_FLARESOLVERR` is set
and FlareSolverr is healthy, or install `curl_cffi`. Check the search engine logs
for `[bt4gprx]` messages.

**`curl_cffi is not installed and FlareSolverr is not configured`.**
You are on the stdlib-only path, which Cloudflare blocks. Configure FlareSolverr
(recommended) or install `curl_cffi` in qBittorrent's Python.

**FlareSolverr errors or timeouts.**
Confirm the URL is `http://localhost:8191/v1` in the namespace setup (not the
service name). Check `docker logs flaresolverr`. Headless Chrome can crash with a
small `/dev/shm`; the compose file sets `shm_size: 1gb` to avoid this.

**Searches are slow or occasionally fail through the VPN.**
FlareSolverr's requests exit through your VPN IP, and datacenter/VPN IPs draw
Cloudflare challenges more often than residential ones. Retrying usually works; a
different VPN endpoint may help.

**The site changed its HTML and parsing broke.**
The page layout is handled in two places in `bt4gprx.py`: the `_search_url()`
method (URL scheme) and the `_ResultParser` class (result markup). Update those if
bt4g changes its site structure.

**Plugin doesn't appear after install.**
Ensure the Search tab is enabled (**View → Search Engine**) and that the image has
a Python interpreter. Restart qBittorrent after manual file installs.