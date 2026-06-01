# qBittorrent Search Plugin — bt4gprx

A qBittorrent search-engine plugin for [bt4gprx.com](https://bt4gprx.com), a DHT
torrent index. Written for **Python 3.12**, it gets past the site's **Cloudflare**
protection by "acting like a human": it reuses a single session (so the
`cf_clearance` cookie persists), sends a full browser header set, spaces requests
out with randomized delays, and can drive a real anti-detection browser
(**Byparr**) to clear JavaScript / Turnstile challenges.

Features:

- Free-text search across the whole site.
- **Category-focused search** — qBittorrent's categories are mapped onto bt4g's
  buckets (all, video, audio, doc, app, other).
- **Cloudflare-resistant fetching** with three transports, tried in order:
  Byparr (solver) → `curl_cffi` (TLS impersonation) → stdlib `urllib`.
- **Manual-cookie fallback** — paste a solved `cf_clearance` to bypass the solver.
- **Auto-updating trackers** — magnets are populated from
  [ngosang/trackerslist](https://github.com/ngosang/trackerslist)
  (`trackers_best.txt`), refreshed live with a baked-in offline fallback.
- **Leveled logging** to a tailable file for easy debugging.

> **Note on the official repo.** Official qBittorrent search plugins are expected
> to use only the Python standard library. Because bt4gprx.com is behind
> Cloudflare, this plugin relies on extra tooling (Byparr and/or `curl_cffi`), so
> it is **not** eligible for the official plugin repository.

---

## Table of contents

1. [Requirements](#requirements)
2. [How Cloudflare bypass works](#how-cloudflare-bypass-works)
3. [Docker Compose setup](#docker-compose-setup)
4. [Installing the plugin](#installing-the-plugin)
5. [Using the plugin](#using-the-plugin)
6. [Environment variables](#environment-variables)
7. [Category mapping](#category-mapping)
8. [Search URL format](#search-url-format)
9. [Debugging](#debugging)
10. [Troubleshooting](#troubleshooting)

---

## Requirements

- qBittorrent with the **Search** tab enabled and a working Python interpreter.
- One of the following for Cloudflare (the plugin still loads without them, but
  will be blocked while Cloudflare is active):
  - **Byparr** running as a reachable service *(recommended for Docker)*, **or**
  - the **`curl_cffi`** package installed in the *same* Python qBittorrent uses
    for search, **or**
  - a manually pasted `cf_clearance` cookie (see `BT4G_CF_CLEARANCE`).

The Docker Compose setup below uses the Byparr route, which means the qBittorrent
container needs **no third-party Python packages at all**.

---

## How Cloudflare bypass works

When the plugin fetches a page it tries these transports in order and uses the
first that returns usable HTML:

1. **Byparr** — only if `BT4G_BYPARR` (or the legacy `BT4G_FLARESOLVERR`) is set.
   A maintained, FlareSolverr-compatible solver that runs a real anti-detection
   browser, so it clears modern Cloudflare "Just a moment" / Turnstile challenges
   that FlareSolverr no longer can. Most human-like, heaviest. Uses only the
   standard library to talk to the solver.
2. **`curl_cffi`** — TLS / JA3 browser impersonation. Lightweight and fast; clears
   Cloudflare when detection is fingerprint-based. Used when no solver is set, and
   always for the tracker-list refresh.
3. **`urllib`** (stdlib) — fallback so the plugin still works if Cloudflare is off.
   Usually blocked while Cloudflare is active.

If `BT4G_CF_CLEARANCE` (or `BT4G_COOKIE`) is set, the plugin **skips the solver**
and sends the cookie on the direct (`curl_cffi`/`urllib`) requests instead.

> **Why Byparr, not FlareSolverr?** FlareSolverr can no longer solve Cloudflare's
> current managed-challenge / Turnstile pages and times out (HTTP 500, "Error
> solving the challenge"). Byparr speaks the same `/v1` API, so it is a drop-in
> replacement — no plugin change is needed to switch.

---

## Docker Compose setup

This example runs qBittorrent behind a **Gluetun** VPN and adds **Byparr** in the
*same network namespace* so the two can talk over `localhost`.

> **Why the same namespace?** qBittorrent uses `network_mode: "service:gluetun"`,
> so it shares Gluetun's network stack. Placing Byparr in that same namespace lets
> qBittorrent reach it at `http://localhost:8191/v1` with no extra firewall rules,
> and Byparr's traffic also egresses through the VPN. If you instead put Byparr on
> a normal bridge network, Gluetun's killswitch will block qBittorrent from
> reaching it unless you add your Docker subnet to `FIREWALL_OUTBOUND_SUBNETS`.

### `docker-compose.yml`

The file can be found [here to look at](docker-compose.yml).

### `.env`

Copy `env.example` to `.env`. Change them to match your env. Please read through 
the [env.example](env.example) file as it does not contain enough info to start 
out of the box!

### Start it

```bash
sudo docker compose up -d
```

Gluetun comes up first; qBittorrent and Byparr start once the VPN is healthy. The
Web UI is at `http://<host>:8080`.

> **Important:** `BT4G_BYPARR` must be `http://localhost:8191/v1` here — **not**
> `http://byparr:8191`. Because the containers share Gluetun's network stack, the
> service name will not resolve from inside the namespace, but `localhost` points
> at the shared stack.

> **VPN caveat:** Byparr's challenge-solving requests exit through your VPN IP.
> Datacenter/VPN IPs draw Cloudflare challenges more often than residential ones,
> so a solve may occasionally be slow or need a retry. If slow, try changing 
> `BT4G_FS_TIMEOUT_MS` to something bigger.

---

## Installing the plugin

Install through the Web UI, which copies the file into the correct engines
directory and persists it in your `/config` volume:

1. Open the qBittorrent Web UI (`http://<host>:8080`).
2. Go to the **Search** tab. If it is missing, enable it under
   **View → Search Engine**, then confirm the image detected Python.
3. Click **Search plugins…** (bottom right) → **Install a new one**.
4. Choose **Web link** and paste the raw URL to `bt4gprx.py`, **or** choose
   **Local file** and point to a path reachable inside the container.
```
https://raw.githubusercontent.com/iamdoubz/g-qb-flare/refs/heads/main/plugins/bt4gprx/bt4gprx.py
```
5. Confirm `bt4gprx` appears in the plugin list and is enabled.

### Alternative: install by file path

```bash
docker cp bt4gprx.py qbittorrent:/config/qBittorrent/nova3/engines/bt4gprx.py
docker restart qbittorrent
```

---

## Using the plugin

1. Open the **Search** tab in the qBittorrent Web UI.
2. Type your search terms.
3. Pick a **category** to focus the search, or leave it on **All categories**.
4. Run the search. Results are sorted with the most seeders first.
5. Double-click a result (or use the download button) to add it.

> **Download timing:** bt4g's search results link to detail pages, not directly to
> magnets. When you download a result, the plugin fetches its detail page (through
> Byparr) to extract the real infohash, then builds the magnet locally with the
> tracker list. Expect a ~30-second pause for that one fetch — it's Byparr clearing
> Cloudflare, not a hang. It happens once per download, only for the item you grab.

### Testing outside qBittorrent

From inside the container, you can run the bundled `nova2.py` harness:

```bash
cd /config/qBittorrent/nova3
python3 nova2.py bt4gprx all ubuntu
python3 nova2.py bt4gprx tv "frieren season 2"
```

Each result is one `|`-separated line:
`link | name | size | seeds | leech | engine_url | desc_link | pub_date`.

---

## Environment variables

All variables are **optional**. Set them on the container that runs qBittorrent's
search (the `qbittorrent` service in the Compose example).

| Variable | Default | Description |
|---|---|---|
| `BT4G_BYPARR` | *(empty)* | Byparr (or FlareSolverr) endpoint, e.g. `http://localhost:8191/v1`. When set, the plugin prefers the solver to clear Cloudflare. |
| `BT4G_FLARESOLVERR` | *(empty)* | Legacy alias for `BT4G_BYPARR` (same effect). `BT4G_BYPARR` takes precedence if both are set. |
| `BT4G_FS_TIMEOUT_MS` | `60000` | Solver `maxTimeout` in milliseconds. Raise it if the solver needs longer to clear a challenge. |
| `BT4G_CF_CLEARANCE` | *(empty)* | Manually solved Cloudflare `cf_clearance` cookie value. When set, the plugin skips the solver and sends this cookie on direct requests. Bound to IP + User-Agent + TLS fingerprint and expires (~30 min); solve from the same exit IP and set `BT4G_USER_AGENT` to match. `curl_cffi` strongly recommended (its browser TLS fingerprint helps the cookie pass). |
| `BT4G_COOKIE` | *(empty)* | Full raw `Cookie` header to send instead (overrides `BT4G_CF_CLEARANCE`). |
| `BT4G_USER_AGENT` | Chrome 124 UA | User-Agent to send; MUST match the browser that solved the challenge when using `BT4G_CF_CLEARANCE`. |
| `BT4G_IMPERSONATE` | `chrome` | `curl_cffi` impersonation target (e.g. `chrome`, `chrome124`, `safari17`). |
| `BT4G_UPDATE_TRACKERS` | `1` | Refresh the tracker list from ngosang/trackerslist at runtime. Set to `0` (also `false`/`no`) to use only the embedded snapshot. |
| `BT4G_MAX_PAGES` | `3` | Maximum number of result pages to walk per search (minimum effective value `1`). |
| `BT4G_MIN_DELAY` | `1.5` | Minimum seconds between requests (lower bound of the randomized human-like delay). |
| `BT4G_MAX_DELAY` | `4.0` | Maximum seconds between requests (upper bound of the delay). |
| `BT4G_BASE_URL` | `https://bt4gprx.com` | Override the site base URL if the domain changes or you use a mirror. |
| `BT4G_PAGE_PARAM` | `p` | Query param name for pagination. Set to `""` to disable paging (single page). |
| `BT4G_SORT_PARAM` | `orderby` | Query param name for sorting. Set to `""` to drop sorting (site default order). |
| `BT4G_SORT_VALUE` | `seeders` | Value for the sort param. |
| `BT4G_LOG_LEVEL` | `ERROR` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` (case-insensitive) or numeric. Use `DEBUG` for verbose tracing while testing. |
| `BT4G_LOG_FILE` | `/config/bt4gprx.log` (then `/tmp/bt4gprx.log`) | Path for a tailable log. Set to `""` to disable file logging. |

---

## Category mapping

qBittorrent has a fixed set of categories. bt4g groups content more coarsely
(valid values: `all`, `video`, `audio`, `doc`, `app`, `other`), so several
qBittorrent categories map onto the same bt4g bucket:

| qBittorrent category | bt4g `category=` value |
|---|---|
| All | `all` |
| Movies | `video` |
| TV | `video` *(bt4g has no separate TV bucket)* |
| Anime | `video` |
| Music | `audio` |
| Books | `doc` |
| Software | `app` |
| Games | `app` |
| Pictures | `other` |

If bt4g changes its categories, edit the `supported_categories` dictionary in
`bt4gprx.py`.

---

## Search URL format

The plugin builds URLs in this confirmed format:

```
https://${BT4G_BASE_URL}/search?q=<term>&category=<cat>&orderby=seeders&p=<page>
```

- Spaces in the term are encoded as `%20`.
- `category` is always sent, including `category=all` for "All categories".
- `orderby` and `p` are configurable/removable via the env vars above.

---

## Debugging

The plugin writes diagnostics to a tailable logfile (default `/config/bt4gprx.log`)
as well as stderr. To debug a search:

```bash
# Set DEBUG on the qbittorrent service, recreate, then:
docker exec -it qbittorrent tail -f /config/bt4gprx.log
```

At `DEBUG` you see the full lifecycle: URL fetched, which transport ran and the
status/byte-length it returned, rows parsed per page, and how each magnet was
resolved. To run a search manually inside the container:

```bash
cd /config/qBittorrent/nova3
python3 nova2.py bt4gprx all ubuntu
```

To capture the exact HTML the plugin sees (through Byparr), POST to the solver:

```bash
docker exec -it qbittorrent python3 - <<'PY'
import json, urllib.request
payload = {"cmd":"request.get",
           "url":"https://bt4gprx.com/search?q=frieren&category=all&orderby=seeders&p=1",
           "maxTimeout":60000}
req = urllib.request.Request("http://localhost:8191/v1",
        data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json"}, method="POST")
html = json.loads(urllib.request.urlopen(req, timeout=90).read())["solution"]["response"]
open("/config/bt4g_sample.html","w").write(html)
print("saved", len(html), "bytes")
PY
docker cp qbittorrent:/config/bt4g_sample.html ./bt4g_sample.html
```

---

## Troubleshooting

**No results at all / every search is empty.**
Make sure `BT4G_BYPARR` is set and Byparr is healthy, or install `curl_cffi`, or
paste a `BT4G_CF_CLEARANCE`. Check the log for `[bt4gprx]` lines.

**Log says "Solver could not clear Cloudflare … challenge timeout".**
The solver reached the site but failed the challenge. If you are still on legacy
FlareSolverr, switch to Byparr (it handles modern challenges). Behind a VPN, a
residential proxy on the solver may also be needed.

**Log says "Solver unreachable".**
Confirm the URL is `http://localhost:8191/v1` in the namespace setup (not the
service name), and check `docker logs byparr`. Headless Chrome can crash with a
small `/dev/shm`; the compose file sets `shm_size: 2gb`.

**Log says "bt4g appears DOWN — Cloudflare error 5xx".**
This is the origin site failing (e.g. 525 = SSL handshake failed), not the plugin
or the solver. Try again later.

**A download does nothing / "result may have been removed".**
The detail page had no infohash — the torrent was likely removed from bt4g. The
log line names the page. Try another result.

**Searches are slow.**
Each search walks up to `BT4G_MAX_PAGES` pages with a randomized 1.5–4s delay
between requests, and each *download* triggers one detail-page solve (~30s). Lower
`BT4G_MAX_PAGES` / the delays while testing if needed.

**The site changed its HTML and parsing broke.**
The layout is handled in two places in `bt4gprx.py`: `_search_url()` (URL scheme)
and the `_ResultParser` class (result markup). The magnet resolution lives in
`_resolve_magnet()`. Update those if bt4g changes its structure.