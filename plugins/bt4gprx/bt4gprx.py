#VERSION: 1.03
# AUTHORS: iamdoubz
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
#   1. Byparr  - only if you opt in via env var BT4G_FLARESOLVERR (e.g.
#                      http://localhost:8191/v1). Runs a headless browser, so it
#                      can clear actual JS / Turnstile challenges. Most human-like,
#                      heaviest. Run it with Docker:
#                        docker run -d --name flaresolverr -p 8191:8191 \
#                          --restart unless-stopped ghcr.io/thephaseless/byparr:latest
#   2. curl_cffi     - TLS/JA3 browser impersonation. Lightweight, fast, and clears
#                      Cloudflare when detection is fingerprint-based (the common
#                      case). This is the default.
#   3. urllib        - stdlib fallback so the plugin still loads/works if Cloudflare
#                      is disabled. Will usually be blocked while CF is active.
#
# OPTIONAL ENVIRONMENT VARIABLES
# ------------------------------
#   BT4G_FLARESOLVERR   Byparr endpoint, e.g. http://localhost:8191/v1
#   BT4G_FS_TIMEOUT_MS  solver maxTimeout in milliseconds, default 60000
#   BT4G_CF_CLEARANCE   manually solved Cloudflare cf_clearance cookie value. When
#                       set, the plugin skips the solver and sends this cookie on
#                       direct (curl_cffi/urllib) requests. Bound to IP + User-Agent
#                       + TLS fingerprint and expires (~30 min); solve from the same
#                       exit IP and set BT4G_USER_AGENT to match. curl_cffi strongly
#                       recommended (its browser TLS fingerprint helps the cookie pass).
#   BT4G_COOKIE         full raw Cookie header to send instead (overrides CF_CLEARANCE)
#   BT4G_USER_AGENT     User-Agent to send; MUST match the browser that solved the
#                       challenge when using BT4G_CF_CLEARANCE
#   BT4G_IMPERSONATE    curl_cffi target, default "chrome" (e.g. chrome124, safari17)
#   BT4G_MAX_PAGES      max result pages to walk, default 3
#   BT4G_MIN_DELAY      min seconds between requests, default 1.5
#   BT4G_MAX_DELAY      max seconds between requests, default 4.0
#   BT4G_BASE_URL       override base URL if the domain/mirror changes
#   BT4G_PAGE_PARAM     query param name for pagination, default "p"; set to ""
#                       to disable paging (single page of results)
#   BT4G_SORT_PARAM     query param name for sorting, default "orderby"; set to
#                       "" to drop sorting from the URL (site default order)
#   BT4G_SORT_VALUE     value for the sort param, default "seeders"
#   BT4G_LOG_FILE       path for a tailable log (default /config/bt4gprx.log, then
#                       /tmp/bt4gprx.log); set to "" to disable file logging
#   BT4G_LOG_LEVEL      DEBUG | INFO | WARNING | ERROR | CRITICAL (default ERROR).
#                       Use DEBUG for verbose tracing while testing.
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
import logging
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

# Targeted patterns for finding the REAL btih on a bt4g /magnet/<id> detail page.
# The genuine hash appears in the button URLs, e.g.
#   //downloadtorrentfile.com/hash/<btih>?name=...
#   //keepshare.org/.../magnet:?xt=urn:btih:<btih>
# IMPORTANT: the "Report Abuse" link carries infohash=<bt4g-internal-id> (the same
# 33-char token as the /magnet/<id> URL, NOT a btih), so that param must be ignored.
# These two contexts unambiguously contain the real hash, so they're tried first.
_DETAIL_INFOHASH_RES = [
    re.compile(r"urn:btih:([A-Fa-f0-9]{40}|[A-Z2-7]{32})"),
    re.compile(r"/hash/([A-Fa-f0-9]{40}|[A-Z2-7]{32})"),
]

# Cloudflare origin-side error codes. When any of these come back, bt4g itself
# (the origin) is the problem, not our request or the bypass.
_CF_ORIGIN_ERRORS = {
    520: "Web server returned an unknown error",
    521: "Web server is down",
    522: "Connection timed out",
    523: "Origin is unreachable",
    524: "A timeout occurred",
    525: "SSL handshake failed",
    526: "Invalid SSL certificate",
    527: "Railgun error",
}

# Where to write a persistent, tailable log. qBittorrent does not reliably forward
# a search plugin's stderr to `docker logs`, so we also append here. Override with
# BT4G_LOG_FILE; set it to "" to disable file logging. Default prefers /config
# (the linuxserver persistent volume), then /tmp.
def _default_log_file():
    env = os.environ.get("BT4G_LOG_FILE")
    if env is not None:
        return env.strip()  # explicit override (may be "" to disable)
    for d in ("/config", "/tmp"):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return os.path.join(d, "bt4gprx.log")
    return ""

_LOG_FILE = _default_log_file()


def _resolve_level():
    """Read BT4G_LOG_LEVEL. Accepts level names (DEBUG/INFO/WARNING/ERROR/CRITICAL,
    case-insensitive) or a numeric value. Defaults to ERROR for released use."""
    raw = os.environ.get("BT4G_LOG_LEVEL", "ERROR").strip()
    if not raw:
        return logging.ERROR
    if raw.isdigit():
        return int(raw)
    return getattr(logging, raw.upper(), logging.ERROR)


def _build_logger():
    """Configure the module logger once. Writes to stderr always, and to the
    tailable logfile when writable. stdout is reserved for search results."""
    lg = logging.getLogger("bt4gprx")
    lg.setLevel(_resolve_level())
    lg.propagate = False  # don't leak to the root logger / qBittorrent's stdout
    if lg.handlers:       # already configured (engine re-instantiated) -> reuse
        return lg
    fmt = logging.Formatter(
        "%(asctime)s [bt4gprx] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    lg.addHandler(sh)
    if _LOG_FILE:
        try:
            fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        except Exception:
            pass  # never let logging setup break a search
    return lg


log = _build_logger()


class bt4gprx(object):
    # These three MUST be class attributes or qBittorrent refuses to install.
    url = os.environ.get("BT4G_BASE_URL", "https://bt4gprx.com")
    name = "bt4gprx"

    # qBittorrent's fixed categories mapped onto bt4g's `category=` query values.
    # bt4g's valid categories are: all, video, audio, doc, app, other. We send no
    # category for "all" (search everything). NOTE: a confirmed example URL showed
    # category=movie, which conflicts with the enumerated set (video); this map
    # uses "video" per the enumerated set — change to "movie" here if needed.
    supported_categories = {
        "all": "",
        "movies": "video",
        "tv": "video",        # bt4g has no separate TV bucket
        "anime": "video",
        "music": "audio",
        "books": "doc",
        "software": "app",
        "games": "app",
        "pictures": "other",  # closest bt4g bucket for images/misc
    }

    def __init__(self):
        self.flaresolverr = os.environ.get("BT4G_FLARESOLVERR", "").strip()
        self.impersonate = os.environ.get("BT4G_IMPERSONATE", "chrome").strip() or "chrome"
        self.max_pages = self._int_env("BT4G_MAX_PAGES", 3)
        self.min_delay = self._float_env("BT4G_MIN_DELAY", 1.5)
        self.max_delay = self._float_env("BT4G_MAX_DELAY", 4.0)
        # Query-string parameter names for the /search endpoint. Confirmed format:
        #   /search?q=<term>&category=<cat>&orderby=seeders&p=<page>
        # All are overridable in case the site changes. Set BT4G_PAGE_PARAM="" to
        # disable paging, or BT4G_SORT_PARAM="" to drop sorting from the URL.
        self.page_param = os.environ.get("BT4G_PAGE_PARAM", "p").strip()
        self.sort_param = os.environ.get("BT4G_SORT_PARAM", "orderby").strip()
        self.sort_value = os.environ.get("BT4G_SORT_VALUE", "seeders").strip()
        # Manual Cloudflare clearance. Solve the challenge in a browser, then paste
        # the cf_clearance cookie value here. IMPORTANT: cf_clearance is bound to
        # the IP, User-Agent and TLS fingerprint that solved it, so (a) solve from
        # the SAME exit IP the plugin uses (behind your VPN, browse via the same
        # endpoint), and (b) set BT4G_USER_AGENT to the exact UA from that browser.
        # The cookie typically expires in ~30 min and must be re-pasted.
        #   BT4G_CF_CLEARANCE  - just the cf_clearance value, or
        #   BT4G_COOKIE        - a full raw Cookie header (overrides CF_CLEARANCE)
        self.user_agent = os.environ.get(
            "BT4G_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ).strip()
        cf = os.environ.get("BT4G_CF_CLEARANCE", "").strip()
        raw_cookie = os.environ.get("BT4G_COOKIE", "").strip()
        if raw_cookie:
            self.cookie = raw_cookie
        elif cf:
            self.cookie = "cf_clearance=%s" % cf
        else:
            self.cookie = ""
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
            "User-Agent": self.user_agent,
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
        if self.cookie:
            h["Cookie"] = self.cookie
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

    @staticmethod
    def _cf_error_in_body(html):
        """If the page is a Cloudflare origin-error screen (e.g. 525), return the
        numeric code, else None. Cloudflare error pages contain markup like
        'Error 525' / 'cf-error-details' near a 5xx code."""
        if not html:
            return None
        head = html[:6000].lower()
        if "cloudflare" not in head and "cf-error" not in head and "error code" not in head:
            return None
        m = re.search(r"\berror[\s:]*?(52[0-7])\b", head)
        if not m:
            m = re.search(r"\b(52[0-7])\b", head)
        return int(m.group(1)) if m else None

    # ---------------------------------------------------------------- fetching
    def _fetch(self, url, referer=None):
        """Return page HTML as str, or None on failure. Tries the best
        available transport and warns clearly if Cloudflare blocks us."""
        self._humanize_delay()
        log.debug("fetch: %s (referer=%s)", url, referer)

        # A manually pasted cf_clearance cookie only works on the direct transports
        # (curl_cffi / urllib) that send it — FlareSolverr uses its own browser
        # session and ignores it. So when a cookie is set, skip FlareSolverr.
        use_flaresolverr = self.flaresolverr and not self.cookie
        if self.flaresolverr and self.cookie:
            log.debug("manual cookie set; skipping FlareSolverr and using direct path")

        if use_flaresolverr:
            log.debug("trying FlareSolverr at %s", self.flaresolverr)
            html, status = self._fetch_flaresolverr(url)
            log.debug("FlareSolverr -> status=%s, html_len=%s",
                      status, len(html) if html else None)
            site_down = self._report_status(status, "FlareSolverr")
            if html is not None:
                # FlareSolverr sometimes reports 200 while the rendered page is
                # actually a Cloudflare error screen; catch that from the body too.
                if not site_down:
                    code = self._cf_error_in_body(html)
                    if code:
                        log.error(
                            "bt4g appears DOWN — Cloudflare error %d (%s) detected in "
                            "page body via FlareSolverr; the origin site is failing.",
                            code, _CF_ORIGIN_ERRORS.get(code, "origin error"),
                        )
                return html
            log.info("FlareSolverr returned no content; falling back.")

        if cffi_requests is not None:
            log.debug("trying curl_cffi (impersonate=%s, cookie=%s)",
                      self.impersonate, "yes" if self.cookie else "no")
            html, status = self._fetch_curl_cffi(url, referer)
            log.debug("curl_cffi -> status=%s, html_len=%s",
                      status, len(html) if html else None)
            if html is not None and not self._looks_like_challenge(html, status):
                return html
            if self.cookie:
                log.warning(
                    "curl_cffi still hit a Cloudflare challenge (status=%s) despite a "
                    "manual cookie. The cf_clearance is likely expired or bound to a "
                    "different IP/User-Agent/TLS fingerprint. Re-solve in a browser on "
                    "the SAME exit IP, paste a fresh BT4G_CF_CLEARANCE, and ensure "
                    "BT4G_USER_AGENT matches that browser.", status
                )
            else:
                log.warning(
                    "curl_cffi got a Cloudflare challenge (status=%s). For JS/Turnstile "
                    "challenges, run a solver (Byparr/FlareSolverr) and set "
                    "BT4G_FLARESOLVERR, or paste a fresh BT4G_CF_CLEARANCE.", status
                )
            if html is not None:
                return html  # hand back anyway; parser will simply find nothing

        # Last resort: stdlib. Almost always blocked while CF is on.
        if cffi_requests is None and not self.flaresolverr:
            log.warning(
                "curl_cffi is not installed and FlareSolverr is not configured. "
                "Using urllib, which Cloudflare will likely block. Install with: "
                "python3 -m pip install curl_cffi"
            )
        log.debug("trying urllib (stdlib) for %s", url)
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
            log.warning("curl_cffi error: %s", e)
            return None, None

    def _fetch_urllib(self, url, referer):
        try:
            req = urllib.request.Request(url, headers=self._headers(referer))
            with urllib.request.urlopen(req, timeout=30) as r:
                charset = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            log.warning("urllib HTTP %s for %s", e.code, url)
        except Exception as e:
            log.warning("urllib error: %s", e)
        return None

    def _fetch_flaresolverr(self, url):
        """Drive a FlareSolverr-compatible solver (FlareSolverr, Byparr, ...) to
        clear CF and return (html, upstream_status). Returns (None, None) on a
        transport error. A solver that *reached us* but failed to solve the
        challenge (HTTP 500 with a JSON message) is reported distinctly so the
        log makes clear the problem is the solver/challenge, not connectivity."""
        timeout_ms = self._int_env("BT4G_FS_TIMEOUT_MS", 60000)
        body = None
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": timeout_ms,
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
            # Read with a margin over the solver's own maxTimeout so we receive
            # its error response (e.g. "Timeout after 60.0s") instead of our own
            # socket timeout masking it as "unreachable".
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0 + 30) as r:
                body = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            # The solver answered, but with an error status. Its body usually
            # carries the real reason (challenge timeout, etc.).
            detail = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
                detail = (json.loads(raw).get("message") or raw).strip()
            except Exception:
                pass
            if "challenge" in detail.lower() or "timeout" in detail.lower():
                log.error(
                    "Solver could not clear Cloudflare for %s (HTTP %s): %s "
                    "FlareSolverr struggles with modern 'Just a moment' / Turnstile "
                    "challenges; consider a maintained solver (e.g. Byparr) or a "
                    "residential proxy.", url, e.code, detail or "(no detail)",
                )
            else:
                log.error("Solver HTTP %s at %s: %s", e.code, self.flaresolverr, detail or e)
            return None, None
        except Exception as e:
            log.error("Solver unreachable at %s: %s", self.flaresolverr, e)
            return None, None

        # FlareSolverr itself reports a status string ("ok"/"error") plus a message.
        if body.get("status") != "ok":
            log.warning("Solver did not return ok: %s", body.get("message", "unknown error"))
        sol = body.get("solution") or {}
        status = sol.get("status")  # the HTTP status bt4g/Cloudflare returned
        return sol.get("response"), status

    @staticmethod
    def _report_status(status, source):
        """Log a clear, human-readable line for a notable HTTP status. Returns
        True if the status indicates the site (not us) is the problem."""
        try:
            code = int(status)
        except (TypeError, ValueError):
            return False
        if code in _CF_ORIGIN_ERRORS:
            log.error(
                "bt4g appears DOWN — Cloudflare error %d (%s) via %s. This is the "
                "origin site failing, not the plugin or the bypass; try again later.",
                code, _CF_ORIGIN_ERRORS[code], source,
            )
            return True
        if code in (403, 429, 503):
            log.warning("Cloudflare challenge/block: HTTP %d via %s.", code, source)
            return True
        if code >= 500:
            log.error("Server error: HTTP %d via %s.", code, source)
            return True
        if code >= 400:
            log.warning("HTTP %d via %s.", code, source)
            return True
        log.debug("HTTP %s via %s (ok).", code, source)
        return False

    # ----------------------------------------------------------------- parsing
    class _ResultParser(HTMLParser):
        """Parses the current bt4g results markup (verified June 2026):
            div.list-group
              div.result-item
                h5 > a[title][href="/magnet/<id>"]   -> title + detail link
                ul > li > span                        -> per-file sizes (ignored)
                p.mb-1
                  span > span.cpill.fileType1         -> category label (ignored)
                  ... "Creation Time:&nbsp;YYYY-MM-DD"
                  ... "Total Size: " <b class="cpill ...-pill">SIZE</b>
                  ... "Seeders: "   <b id="seeders">N</b>
                  ... "Leechers: "  <b id="leechers">N</b>
        Note the SIZE pill is a <b> (the category pill is a <span>), so matching
        tag=='b' with a 'cpill' class isolates the total size. A row is emitted
        when the (final) leechers value is read. If the site changes again, this
        class and _search_url are the two places to update."""

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.in_item = False
            self.cur = {}
            self.expect = None          # 'filesize' | 'seeders' | 'leechers'
            self.in_meta_span = False    # inside a <span> within the <p> meta row
            self.results = []

        def parse(self, html):
            self.feed(html)
            return self.results

        def handle_starttag(self, tag, attrs):
            a = {k: (v or "") for k, v in attrs}
            cls = a.get("class", "")
            if tag == "div" and "result-item" in cls:
                self.in_item = True
                self.cur = {}
                self.expect = None
                return
            if not self.in_item:
                return
            if tag == "a" and a.get("href") and "title" in a and "title" not in self.cur:
                self.cur["title"] = a["title"]
                self.cur["href"] = a["href"]
            elif tag == "b":
                if a.get("id") == "seeders":
                    self.expect = "seeders"
                elif a.get("id") == "leechers":
                    self.expect = "leechers"
                elif "cpill" in cls:        # the <b> total-size pill
                    self.expect = "filesize"
            elif tag == "span":
                # The meta row holds "Creation Time:&nbsp;<date>" as span text.
                self.in_meta_span = True

        def handle_endtag(self, tag):
            if tag == "span":
                self.in_meta_span = False

        def handle_data(self, data):
            if not self.in_item:
                return
            text = data.strip()
            if not text:
                return
            if self.expect:
                if self.expect == "filesize":
                    self.cur["filesize"] = text
                elif self.expect == "seeders":
                    self.cur["seeders"] = text
                elif self.expect == "leechers":
                    self.cur["leechers"] = text
                    if self.cur.get("title") and self.cur.get("href"):
                        self.results.append(self.cur)
                    self.in_item = False
                    self.cur = {}
                self.expect = None
                return
            # Opportunistically capture the creation date for pub_date.
            if self.in_meta_span and "creation time" in text.lower():
                m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                if m:
                    self.cur["pub_date_str"] = m.group(1)

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
            log.info("tracker list refresh failed (%s); using embedded snapshot.", e)
            return
        fetched = [ln.strip() for ln in text.splitlines() if ln.strip()]
        # Only adopt it if it looks sane (a few real tracker URLs).
        fetched = [t for t in fetched if "://" in t]
        if len(fetched) >= 3:
            self._trackers = fetched
            log.debug("loaded %d trackers from trackerslist.", len(fetched))

    def _build_magnet(self, info_hash, display_name=None):
        self._load_trackers()
        m = "magnet:?xt=urn:btih:%s" % info_hash
        if display_name:
            m += "&dn=%s" % quote(display_name)
        for tr in self._trackers:
            m += "&tr=%s" % quote(tr)
        return m

    @staticmethod
    def _date_to_ts(date_str):
        """Convert a 'YYYY-MM-DD' string to a Unix timestamp, or -1 if absent."""
        if not date_str:
            return -1
        try:
            return int(time.mktime(time.strptime(date_str, "%Y-%m-%d")))
        except (ValueError, OverflowError):
            return -1

    def _hash_from_href(self, href):
        match = _HASH_RE.search(href or "")
        return match.group(1) if match else None

    def _resolve_magnet(self, detail_url, display_name=None):
        """Open a result's /magnet/<id> detail page and build a magnet from the
        real btih embedded in its download buttons. Used lazily (only for items
        the user actually downloads) to keep request volume low. We never call
        downloadtorrentfile.com / keepshare.org — the hash is read straight from
        the page and the magnet is assembled locally with our own tracker list."""
        html = self._fetch(detail_url, referer=self.url)
        if not html:
            log.error("could not load detail page %s", detail_url)
            return None

        # Preferred: the genuine btih from the magnet/torrent button URLs.
        for rx in _DETAIL_INFOHASH_RES:
            m = rx.search(html)
            if m:
                info_hash = m.group(1)
                log.debug("resolved btih %s from %s", info_hash, detail_url)
                return self._build_magnet(info_hash, display_name)

        # Fallbacks: a literal magnet link, then any bare hash on the page.
        m = _MAGNET_RE.search(html)
        if m:
            log.debug("resolved literal magnet link from %s", detail_url)
            return m.group(0)
        h = _HASH_RE.search(html)
        if h:
            log.debug("resolved bare infohash %s from %s", h.group(1), detail_url)
            return self._build_magnet(h.group(1), display_name)

        log.error(
            "no btih found on %s (len=%d) — the result may have been removed, or "
            "the detail-page markup changed.", detail_url, len(html)
        )
        return None

    # ------------------------------------------------------------------- search
    def _search_url(self, what, cat, page):
        # Confirmed format:
        #   https://bt4gprx.com/search?q=<term>&category=<cat>&orderby=seeders&p=<n>
        # `what` arrives URL-encoded from nova2 (quote -> spaces as %20). Normalize
        # any '+' (from a quote_plus variant) to %20 so spaces are always %20, as
        # the site requires. A real '+' in the term is %2B and is left untouched.
        q = what.replace("+", "%20").replace(" ", "%20")
        base = self.url.rstrip("/") + "/search"
        params = ["q=%s" % q]
        category = self.supported_categories.get(cat, "")
        if category:
            params.append("category=%s" % category)
        if self.sort_param and self.sort_value:
            params.append("%s=%s" % (self.sort_param, self.sort_value))
        if self.page_param and page:
            params.append("%s=%d" % (self.page_param, page))
        return base + "?" + "&".join(params)

    def _search_page(self, what, cat, page):
        url = self._search_url(what, cat, page)
        html = self._fetch(url, referer=self.url)
        if not html:
            log.debug("page %d: no html returned", page)
            return []
        try:
            rows = self._ResultParser().parse(html)
            log.debug("page %d: parsed %d row(s) from %d bytes", page, len(rows), len(html))
            return rows
        except Exception as e:
            log.warning("parse error on page %d: %s", page, e)
            return []

    # DO NOT change the name/parameters of this function. nova2.py calls it.
    # `what` arrives already URL-escaped (e.g. "Big+Buck+Bunny").
    def search(self, what, cat="all"):
        if cat not in self.supported_categories:
            log.debug("unknown category %r; using 'all'", cat)
            cat = "all"
        log.debug("search start: what=%r cat=%r max_pages=%d", what, cat, self.max_pages)

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
            log.debug("page %d: %d new unique row(s) (total=%d)", page, new, len(rows))
            if new == 0:
                break

        # Convention: most seeds first.
        def _to_int(x):
            try:
                return int(str(x).replace(",", ""))
            except (TypeError, ValueError):
                return -1

        rows.sort(key=lambda r: _to_int(r.get("seeders", -1)), reverse=True)
        log.debug("search done: emitting %d result(s)", len(rows))

        for r in rows:
            href = r["href"]
            desc_link = urljoin(self.url + "/", href)
            # Fast path: if the detail URL already carries the info hash, build the
            # magnet now (no extra request). Otherwise hand qBittorrent the detail
            # page and resolve lazily in download_torrent().
            info_hash = self._hash_from_href(href)
            if info_hash:
                link = self._build_magnet(info_hash, r.get("title"))
                log.debug("result %r: magnet from hash %s", r.get("title"), info_hash)
            else:
                link = desc_link
                log.debug("result %r: deferred magnet (desc page)", r.get("title"))

            prettyPrinter({
                "link": link,
                "name": r.get("title", "-1"),
                "size": r.get("filesize", "-1"),
                "seeds": r.get("seeders", "-1"),
                "leech": r.get("leechers", "-1"),
                "engine_url": self.url,
                "desc_link": desc_link,
                "pub_date": self._date_to_ts(r.get("pub_date_str")),
            })

    # Called by qBittorrent only when `link` was a description page (the slow
    # path above). Must PRINT the resolved magnet (or a file path) to stdout.
    def download_torrent(self, info):
        log.debug("download_torrent: resolving %s", info)
        magnet = self._resolve_magnet(info)
        if magnet:
            print(magnet + " " + info)
        else:
            log.error("could not resolve a magnet from %s", info)


if __name__ == "__main__":
    # Tiny manual harness: `python3 bt4gprx.py <category> <terms...>`
    engine = bt4gprx()
    _cat = sys.argv[1] if len(sys.argv) > 1 else "all"
    _terms = "+".join(sys.argv[2:]) or "ubuntu"
    engine.search(_terms, _cat)