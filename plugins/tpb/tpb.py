# VERSION: 1.02
# AUTHORS: iamdoubz
#
# The Pirate Bay search engine for qBittorrent, backed by the apibay.org JSON API.
#
# Features:
#   * Categories (movies, tv, music, games, software, books, pictures, anime).
#   * Top 100 / Recent browsing via magic search terms (see below).
#   * IMDb support: search by IMDb id, IMDb id shown in result names,
#     and an optional "movies/tv must have an IMDb id" filter.
#
# Magic search terms (type these in the search box):
#   top100   (or: top)      -> Top 100 for the selected category
#   recent   (or: latest)   -> 100 most recently added torrents (any category)
#   tt1234567               -> search by IMDb id (exact title matches)
#
# Speed notes:
#   * One HTTP request per category code (categories that map to a single code
#     are a single request). Codes are fetched in parallel.
#   * Magnet links are built locally from the info_hash returned by the API,
#     so there is NO per-result detail request.
#
# Install: drop this file in qBittorrent's search engines folder, e.g.
#   Linux:   ~/.local/share/qBittorrent/nova3/engines/
#   Windows: %LOCALAPPDATA%\qBittorrent\nova3\engines\
#   macOS:   ~/Library/Application Support/qBittorrent/nova3/engines/
# then enable it under View > Search > Search plugins.

import gzip
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from novaprinter import prettyPrinter
except Exception:  # allows running the file standalone for quick testing
    def prettyPrinter(d):
        keys = ('link', 'name', 'size', 'seeds', 'leech',
                'engine_url', 'desc_link', 'pub_date')
        print('|'.join(str(d.get(k, '')) for k in keys))

try:
    from helpers import retrieve_url  # honors qBittorrent's proxy / settings
except Exception:
    retrieve_url = None


# ---- tweakable options ------------------------------------------------------

# Append the IMDb id to the result name, e.g. "Some.Movie.1080p [tt1234567]".
# The id is only added to the displayed name; the magnet's torrent name stays clean.
SHOW_IMDB_IN_NAME = True

# When True, movies/tv searches only return results that carry an IMDb id.
# This filters out fakes/mislabeled junk but also drops legit untagged releases.
# It is NOT applied to other categories (music/software/etc. never have IMDb ids).
REQUIRE_IMDB_FOR_VIDEO = False

# -----------------------------------------------------------------------------

# Trackers appended to every magnet (the set The Pirate Bay's own site uses).
# Dead trackers are harmless; clients just ignore them.
_TRACKERS = [
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.stealth.si:80/announce',
    'udp://tracker.coppersurfer.tk:6969/announce',
    'udp://tracker.leechers-paradise.org:6969/announce',
    'udp://tracker.dler.org:6969/announce',
    'udp://exodus.desync.com:6969/announce',
    'udp://open.demonii.com:1337/announce',
]
_TRACKERS_QS = ''.join('&tr=' + urllib.parse.quote(t, safe='') for t in _TRACKERS)

_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

_IMDB_RE = re.compile(r'^tt\d{7,8}$')


class tpb(object):
    url = 'https://thepiratebay.org'
    api = 'https://apibay.org'
    name = 'The Pirate Bay'

    # qBittorrent category -> apibay category id(s) to query (comma-separated).
    # Empty value means "no category filter" (search everything).
    supported_categories = {
        'all':      '',
        'movies':   '201,207',   # Movies + HD Movies
        'tv':       '205,208',   # TV shows + HD TV shows
        'music':    '100',       # all Audio
        'games':    '400',       # all Games
        'software': '300',       # all Applications
        'books':    '601,602',   # E-books + Comics
        'pictures': '603,604',   # Pictures + Covers
        'anime':    '',          # TPB has no anime category -> search all
    }

    # qBittorrent category -> ordered Top-100 file candidates. The first one
    # that returns results is used (fallback guards any missing precompiled file).
    top100_categories = {
        'all':      ['all'],
        'movies':   ['201', '207', '200', 'all'],
        'tv':       ['205', '208', '200', 'all'],
        'music':    ['100', 'all'],
        'games':    ['400', 'all'],
        'software': ['300', 'all'],
        'books':    ['601', '602', '600', 'all'],
        'pictures': ['603', '604', '600', 'all'],
        'anime':    ['all'],
    }

    def search(self, what, cat='all'):
        mode = self._special(what)

        if mode == 'recent':
            url = self.api + '/precompiled/data_top100_recent.json'
            self._output(self._get_json(url), sort=False)
            return

        if mode == 'top100':
            self._output(self._top100(cat), sort=False)
            return

        # Normal search: query each category code in parallel, then merge.
        codes = [c for c in self.supported_categories.get(cat, '').split(',') if c]
        queries = codes if codes else ['']  # '' -> request with no &cat=

        rows = []
        with ThreadPoolExecutor(max_workers=max(1, len(queries))) as pool:
            for batch in pool.map(lambda c: self._api(c, what), queries):
                rows.extend(batch)

        require_imdb = REQUIRE_IMDB_FOR_VIDEO and cat in ('movies', 'tv')
        self._output(rows, sort=True, require_imdb=require_imdb)

    # --- internals -------------------------------------------------------

    def _special(self, what):
        q = urllib.parse.unquote(what).strip().lower()
        if q in ('top', 'top100', ':top', 'top:100', '!top'):
            return 'top100'
        if q in ('recent', 'latest', ':recent', 'top:recent',
                 'top100:recent', '!recent'):
            return 'recent'
        return None

    def _top100(self, cat):
        for cid in self.top100_categories.get(cat, ['all']):
            rows = self._get_json(self.api + '/precompiled/data_top100_%s.json' % cid)
            if rows:
                return rows
        return []

    def _output(self, rows, sort=True, require_imdb=False):
        seen = set()
        out = []
        for t in rows:
            info_hash = str(t.get('info_hash', '')).lower()
            # Skip the API's "no results" sentinel and malformed rows.
            if (not info_hash or info_hash == '0' * 40
                    or str(t.get('id', '0')) == '0'):
                continue
            if require_imdb and not self._imdb(t):
                continue
            if info_hash in seen:
                continue
            seen.add(info_hash)
            out.append(t)

        if sort:
            out.sort(key=lambda t: self._int(t.get('seeders')), reverse=True)
        for t in out:
            self._emit(t)

    def _emit(self, t):
        name = t.get('name', '')
        info_hash = t.get('info_hash', '')
        imdb = self._imdb(t)

        display = name
        if SHOW_IMDB_IN_NAME and imdb:
            display = name + ' [' + imdb + ']'

        magnet = ('magnet:?xt=urn:btih:' + info_hash
                  + '&dn=' + urllib.parse.quote(name, safe='')
                  + _TRACKERS_QS)

        prettyPrinter({
            'link': magnet,
            'name': display,
            'size': str(t.get('size', 0)),        # bytes; qBittorrent formats it
            'seeds': self._int(t.get('seeders')),
            'leech': self._int(t.get('leechers')),
            'engine_url': self.url,
            'desc_link': self.url + '/description.php?id=' + str(t.get('id', '')),
            'pub_date': self._int(t.get('added')),  # unix timestamp
        })

    def _api(self, code, what):
        # `what` is already URL-encoded by qBittorrent's nova2 launcher.
        # apibay also accepts an IMDb id directly as the query (q=tt1234567).
        url = self.api + '/q.php?q=' + what + (('&cat=' + code) if code else '')
        return self._get_json(url)

    def _get_json(self, url):
        try:
            data = json.loads(self._get(url))
            return data if isinstance(data, list) else []
        except Exception:
            return []  # a failed request yields nothing instead of breaking search

    def _get(self, url):
        # Prefer qBittorrent's helper (proxy/connection-aware). Fall back to a
        # direct request with a browser UA if it's unavailable or blocked.
        if retrieve_url is not None:
            try:
                raw = retrieve_url(url)
                if raw and raw.lstrip().startswith('['):
                    return raw
            except Exception:
                pass
        req = urllib.request.Request(
            url, headers={'User-Agent': _USER_AGENT, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if resp.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
        return raw.decode('utf-8', 'replace')

    @staticmethod
    def _imdb(t):
        value = (t.get('imdb') or '').strip()
        return value if _IMDB_RE.match(value) else ''

    @staticmethod
    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


if __name__ == '__main__':
    import sys
    engine = tpb()
    args = sys.argv[1:]
    category = 'all'
    if len(args) >= 2 and args[0] in engine.supported_categories:
        category, args = args[0], args[1:]
    engine.search(urllib.parse.quote(' '.join(args) or 'ubuntu'), category)