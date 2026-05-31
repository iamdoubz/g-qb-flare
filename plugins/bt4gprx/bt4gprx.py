#VERSION: 3.00
# AUTHORS: (you)
# LICENSING INFORMATION
#
# qBittorrent search plugin for bt4gprx.com (a DHT torrent index).
#
# WHY THIS ISN'T A "STDLIB ONLY" PLUGIN
# -------------------------------------
# The official qBittorrent guidance says plugins should import only the Python
# standard library, because third-party packages aren't guaranteed to be present.
# bt4gprx.com sits behind Cloudflare, and a plain urllib request (what the bundled
# helpers.retrieve_url uses) is blocked on TLS fingerprint alone. To "act like a
# human" we need either TLS impersonation or a real browser, so this plugin depends
# on third-party tooling. Consequences:
#   * It will NOT be accepted into the official search-plugins repo.
#   * You must install the dependency into the SAME Python that qBittorrent uses
#     for search (Options -> Search shows the detected interpreter; usually the
#     `python3` on your PATH). For example:
#         python3 -m pip install "curl_cffi>=0.7"
#
# FETCH STRATEGY (tried in this order)
# ------------------------------------
#   1. FlareSolverr  - only if you opt in via env var BT4G_FLARESOLVERR (e.g.
#                      http://localhost:8191/v1). Runs a headless browser, so it
#                      can clear actual JS / Turnstile challenges. Most human-like,
#                      heaviest. Run it with Docker:
#                        docker run -d --name flaresolverr -p 8191:8191 \
#                          --restart unless-stopped ghcr.io/flaresolverr/flaresolverr:latest
#   2. curl_cffi     - TLS/JA3 browser impersonation. Lightweight, fast, and clears
#                      Cloudflare when detection is fingerprint-based (the common
#                      case). This is the default.
#   3. urllib        - stdlib fallback so the plugin still loads/works if Cloudflare
#                      is disabled. Will usually be blocked while CF is active.
#
# OPTIONAL ENVIRONMENT VARIABLES
# ------------------------------
#   BT4G_FLARESOLVERR   FlareSolverr endpoint, e.g. http://localhost:8191/v1
#   BT4G_IMPERSONATE    curl_cffi target, default "chrome" (e.g. chrome124, safari17)
#   BT4G_MAX_PAGES      max result pages to walk, default 3
#   BT4G_MIN_DELAY      min seconds between requests, default 1.5
#   BT4G_MAX_DELAY      max seconds between requests, default 4.0
#   BT4G_BASE_URL       override base URL if the domain/mirror changes
#   BT4G_UPDATE_TRACKERS  refresh trackers from ngosang/trackerslist at runtime;
#                         "1" (default) on, "0" to use only the embedded snapshot
#
# TESTING (before installing in qBittorrent)
# ------------------------------------------
# Drop this file next to qBittorrent's nova2.py (or its engines/ folder) and run:
#       python3 nova2.py bt4gprx all ubuntu
#       python3 nova2.py bt4gprx movies "big buck bunny"
# Each result prints one line: link|name|size|seeds|leech|engine_url|desc_link|pub_date

import os
import re
import sys
import json
import time
import random
from html.parser import HTMLParser
from urllib.parse import urljoin, quote

# Bundled helpers (present in qBittorrent's nova3 environment). Imported
# defensively so this file can also be imported/linted standalone.
try:
    from novaprinter import prettyPrinter
except Exception:  # pragma: no cover - only when run outside qBittorrent
    def prettyPrinter(d):
        print("{link}|{name}|{size}|{seeds}|{leech}|{engine_url}|{desc_link}|{pub_date}".format(**d))

# Optional TLS-impersonation client. Absence is handled at runtime.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:
    cffi_requests = None

# Stdlib fallback fetcher.
import urllib.request
import urllib.error


# A small set of well-known public trackers, used only when we have to build a
# magnet from a bare info hash and the page didn't already give us a full magnet.
#
# This is a snapshot of ngosang/trackerslist `trackers_best.txt` (fetched
# 2026-05-30). By default the plugin also refreshes this list live at runtime
# from the URL below; this embedded copy is the offline fallback. Disable the
# live refresh with env var BT4G_UPDATE_TRACKERS=0.
_TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
_DEFAULT_TRACKERS = [
    "udp://zer0day.ch:1337/announce",
    "udp://tracker.publictracker.xyz:6969/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://tracker.t-1.org:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://tracker.plx.im:6969/announce",
    "udp://tracker.iperson.xyz:6969/announce",
    "udp://tracker.auctor.tv:6969/announce",
    "udp://tracker.004430.xyz:1337/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://torrents.tmtime.dev:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://bittorrent-tracker.e-n-c-r-y-p-t.net:1337/announce",
    "https://tracker.zhuqiy.com:443/announce",
    "https://tracker.yemekyedim.com:443/announce",
]

# 40-char hex (btih v1) or 32-char base32 info hash.
_HASH_RE = re.compile(r"\b([A-Fa-f0-9]{40}|[A-Z2-7]{32})\b")
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\"'<>\s]*")


def _log(msg):
    """All diagnostics go to stderr. stdout is reserved for results."""
    print("[bt4gprx] %s" % msg, file=sys.stderr)


class bt4gprx(object):
    # These three MUST be class attributes or qBittorrent refuses to install.
    url = os.environ.get("BT4G_BASE_URL", "https://bt4gprx.com")
    name = "bt4gprx"

    # qBittorrent's fixed category set mapped onto bt4g's path prefixes.
    # bt4g groups things coarsely; where it has no distinct bucket we fall back
    # to '' (search everything). Adjust freely if the site adds categories.
    supported_categories = {
        "all": "",
        "movies": "movie/",
        "tv": "movie/",       # bt4g has no separate TV bucket; it's all "movie/"
        "anime": "movie/",
        "music": "audio/",
        "books": "doc/",
        "software": "app/",
        "games": "app/",
        "pictures": "",       # no dedicated image bucket; search all
    }

    def __init__(self):
        self.flaresolverr = os.environ.get("BT4G_FLARESOLVERR", "").strip()
        self.impersonate = os.environ.get("BT4G_IMPERSONATE", "chrome").strip() or "chrome"
        self.max_pages = self._int_env("BT4G_MAX_PAGES", 3)
        self.min_delay = self._float_env("BT4G_MIN_DELAY", 1.5)
        self.max_delay = self._float_env("BT4G_MAX_DELAY", 4.0)
        self._session = None          # lazy curl_cffi session (keeps cf_clearance)
        self._fs_session = None       # lazy FlareSolverr session id
        self._trackers = list(_DEFAULT_TRACKERS)
        self._update_trackers = os.environ.get("BT4G_UPDATE_TRACKERS", "1").strip() not in ("0", "false", "no", "")
        self._trackers_loaded = False
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _int_env(key, default):
        try:
            return max(1, int(os.environ.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_env(key, default):
        try:
            return max(0.0, float(os.environ.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _humanize_delay(self):
        """Sleep a randomized interval so requests aren't metronome-regular."""
        gap = time.time() - self._last_request_ts
        wait = random.uniform(self.min_delay, self.max_delay) - gap
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.time()

    def _headers(self, referer=None):
        h = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            h["Referer"] = referer
        return h

    @staticmethod
    def _looks_like_challenge(html, status):
        if status in (403, 429, 503):
            return True
        if not html:
            return True
        needle = html[:4000].lower()
        return (
            "just a moment" in needle
            or "cf-chl" in needle
            or "challenge-platform" in needle
            or "enable javascript and cookies" in needle
        )

    # ---------------------------------------------------------------- fetching
    def _fetch(self, url, referer=None):
        """Return page HTML as str, or None on failure. Tries the best
        available transport and warns clearly if Cloudflare blocks us."""
        self._humanize_delay()

        if self.flaresolverr:
            html = self._fetch_flaresolverr(url)
            if html is not None:
                return html
            _log("FlareSolverr fetch failed; falling back.")

        if cffi_requests is not None:
            html, status = self._fetch_curl_cffi(url, referer)
            if html is not None and not self._looks_like_challenge(html, status):
                return html
            _log(
                "curl_cffi got a Cloudflare challenge (status=%s). For JS/Turnstile "
                "challenges, run FlareSolverr and set BT4G_FLARESOLVERR." % status
            )
            if html is not None:
                return html  # hand back anyway; parser will simply find nothing

        # Last resort: stdlib. Almost always blocked while CF is on.
        if cffi_requests is None and not self.flaresolverr:
            _log(
                "curl_cffi is not installed and FlareSolverr is not configured. "
                "Using urllib, which Cloudflare will likely block. Install with: "
                "python3 -m pip install curl_cffi"
            )
        return self._fetch_urllib(url, referer)

    def _get_session(self):
        if self._session is None:
            self._session = cffi_requests.Session(impersonate=self.impersonate)
        return self._session

    def _fetch_curl_cffi(self, url, referer):
        try:
            sess = self._get_session()
            resp = sess.get(
                url,
                headers=self._headers(referer),
                impersonate=self.impersonate,
                timeout=30,
                allow_redirects=True,
            )
            return resp.text, resp.status_code
        except Exception as e:
            _log("curl_cffi error: %s" % e)
            return None, None

    def _fetch_urllib(self, url, referer):
        try:
            req = urllib.request.Request(url, headers=self._headers(referer))
            with urllib.request.urlopen(req, timeout=30) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            _log("urllib HTTP %s for %s" % (e.code, url))
        except Exception as e:
            _log("urllib error: %s" % e)
        return None

    def _fetch_flaresolverr(self, url):
        """Drive a FlareSolverr instance (headless browser) to clear CF and
        return the rendered HTML."""
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
            }
            if self._fs_session:
                payload["session"] = self._fs_session
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.flaresolverr,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                body = json.loads(r.read().decode("utf-8", errors="replace"))
            sol = body.get("solution") or {}
            return sol.get("response")
        except Exception as e:
            _log("FlareSolverr error: %s" % e)
            return None

    # ----------------------------------------------------------------- parsing
    class _ResultParser(HTMLParser):
        """Walks the bt4g results list. Each result is an <a title=... href=...>
        inside div.container, with <b id="seeders">, <b id="leechers"> and a
        <b class="cpill ...">SIZE</b>. (Markup verified against the 2024 layout;
        if the site changes, this is the one method to update.)"""

        def __init__(self):
            super().__init__()
            self.in_container = False
            self.in_entry = False
            self.cur_key = ""
            self.temp = {}
            self.results = []

        def parse(self, html):
            self.feed(html)
            return self.results

        def handle_starttag(self, tag, attrs):
            a = {k: (v or "") for k, v in attrs}
            if tag == "div" and not self.in_container and a.get("class") == "container":
                self.in_container = True
            elif tag == "a" and self.in_container and "title" in a and "href" in a:
                self.in_entry = True
                self.temp = {"title": a["title"], "href": a["href"]}
            elif tag == "b" and self.in_entry:
                cls = a.get("class", "")
                self.cur_key = "filesize" if "cpill" in cls else a.get("id", "")

        def handle_endtag(self, tag):
            if tag == "div" and self.in_entry:
                self.in_entry = False

        def handle_data(self, data):
            if self.in_entry and self.cur_key:
                self.temp[self.cur_key] = data.strip()
                # leechers is the last field per entry -> the row is complete.
                if self.cur_key == "leechers":
                    if self.temp.get("title") and self.temp.get("href"):
                        self.results.append(self.temp)
                    self.temp = {}
                self.cur_key = ""

    # ------------------------------------------------------------------- magnet
    def _load_trackers(self):
        """Refresh the tracker list from ngosang/trackerslist once per run.
        Silently keeps the embedded snapshot on any failure. This fetch goes to
        raw.githubusercontent.com (no Cloudflare), so it skips the human-pacing
        logic used for the torrent site itself."""
        if self._trackers_loaded:
            return
        self._trackers_loaded = True
        if not self._update_trackers:
            return
        text = None
        try:
            if cffi_requests is not None:
                text = cffi_requests.get(_TRACKERS_URL, impersonate=self.impersonate, timeout=15).text
            else:
                req = urllib.request.Request(_TRACKERS_URL, headers={"User-Agent": "qbt-bt4gprx"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    text = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            _log("tracker list refresh failed (%s); using embedded snapshot." % e)
            return
        fetched = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # Only adopt it if it looks sane (a few real tracker URLs).
        fetched = [t for t in fetched if "://" in t]
        if len(fetched) >= 3:
            self._trackers = fetched
            _log("loaded %d trackers from trackerslist." % len(fetched))

    def _build_magnet(self, info_hash, display_name=None):
        self._load_trackers()
        m = "magnet:?xt=urn:btih:%s" % info_hash
        if display_name:
            m += "&dn=%s" % quote(display_name)
        for tr in self._trackers:
            m += "&tr=%s" % quote(tr)
        return m

    def _hash_from_href(self, href):
        match = _HASH_RE.search(href or "")
        return match.group(1) if match else None

    def _resolve_magnet(self, detail_url, display_name=None):
        """Open a single result's detail page and pull out a magnet. Used lazily
        (only for items the user actually downloads) to keep request volume low."""
        html = self._fetch(detail_url, referer=self.url)
        if not html:
            return None
        m = _MAGNET_RE.search(html)
        if m:
            return m.group(0)
        h = _HASH_RE.search(html)
        if h:
            return self._build_magnet(h.group(1), display_name)
        return None

    # ------------------------------------------------------------------- search
    def _search_url(self, what, cat, page):
        prefix = self.supported_categories.get(cat, "")
        base = self.url.rstrip("/") + "/"
        return "%s%ssearch/%s/byseeders/%d" % (base, prefix, what, page)

    def _search_page(self, what, cat, page):
        url = self._search_url(what, cat, page)
        html = self._fetch(url, referer=self.url)
        if not html:
            return []
        try:
            return self._ResultParser().parse(html)
        except Exception as e:
            _log("parse error on page %d: %s" % (page, e))
            return []

    # DO NOT change the name/parameters of this function. nova2.py calls it.
    # `what` arrives already URL-escaped (e.g. "Big+Buck+Bunny").
    def search(self, what, cat="all"):
        if cat not in self.supported_categories:
            cat = "all"

        rows = []
        seen = set()
        for page in range(1, self.max_pages + 1):
            page_rows = self._search_page(what, cat, page)
            if not page_rows:
                break
            new = 0
            for r in page_rows:
                key = r.get("href")
                if key and key not in seen:
                    seen.add(key)
                    rows.append(r)
                    new += 1
            if new == 0:
                break

        # Convention: most seeds first.
        def _to_int(x):
            try:
                return int(str(x).replace(",", ""))
            except (TypeError, ValueError):
                return -1

        rows.sort(key=lambda r: _to_int(r.get("seeders", -1)), reverse=True)

        for r in rows:
            href = r["href"]
            desc_link = urljoin(self.url + "/", href)
            # Fast path: if the detail URL already carries the info hash, build the
            # magnet now (no extra request). Otherwise hand qBittorrent the detail
            # page and resolve lazily in download_torrent().
            info_hash = self._hash_from_href(href)
            if info_hash:
                link = self._build_magnet(info_hash, r.get("title"))
            else:
                link = desc_link

            prettyPrinter({
                "link": link,
                "name": r.get("title", "-1"),
                "size": r.get("filesize", "-1"),
                "seeds": r.get("seeders", "-1"),
                "leech": r.get("leechers", "-1"),
                "engine_url": self.url,
                "desc_link": desc_link,
                "pub_date": -1,
            })

    # Called by qBittorrent only when `link` was a description page (the slow
    # path above). Must PRINT the resolved magnet (or a file path) to stdout.
    def download_torrent(self, info):
        magnet = self._resolve_magnet(info)
        if magnet:
            print(magnet + " " + info)
        else:
            _log("could not resolve a magnet from %s" % info)


if __name__ == "__main__":
    # Tiny manual harness: `python3 bt4gprx.py <category> <terms...>`
    engine = bt4gprx()
    _cat = sys.argv[1] if len(sys.argv) > 1 else "all"
    _terms = "+".join(sys.argv[2:]) or "ubuntu"
    engine.search(_terms, _cat)