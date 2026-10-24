"""Job portals: search(portal, term) reads one portal's list of offers for a
search term, details(offer) the full text of one offer. Standard library
only.

Every portal is a JSON file in portals/ (and in config.PORTALS_DIR, which
may add portals or replace shipped ones by their id):

    {"id", "name", "country", "site" (its address, for the page),
     "default": whether users who chose no portals get it,
     "delay": seconds at least between two requests to it,
     and either "parser": one of the parsers in sites.py, for portals that
     need code, with what it takes (e.g. its addresses), or "search" and
     "fields" (and "details"): the portal described, for portals whose
     lists come as JSON (an API, or JSON in the page); or "import" only,
     for portals the app can't read, whose pages the user sends from
     their browser (importer.py): {"hosts": its domains, "offer": a regex
     an offer's own page's address matches, "key": one whose group is its
     id, "url": an offer's address from {key}, if it should be made so,
     and for the bookmarklet (web/importer.js): "link": a regex a link to
     an offer matches, its first non-empty group the offer's id, "ids":
     attributes that hold an offer's id (tiles not drawn yet have only
     these)}}

A described portal:

    "search": {"url", "params" (templates; {term} is the search term),
               "headers", "format": json, next_data, json_ld or
               script:<id> (where the JSON is), "items": the path to the
               list of offers, "filter": true when the portal can't be
               searched (its offers are kept if they have every word of
               the term)}
    "fields": {offer field: a path in an item ("a.b", "a[].b" for each
               of a list), or {"path"/"template"/"value"/"pairs", and
               "split": a separator, "map": {value: ours} (values not in
               it are dropped)}}; offer fields as below, plus "period"
               (salary per hour, day, month or year) and "text" (the
               offer's full text, when the list has it)
    "details": {"url" (a template), "headers", "format", "text": [paths]};
               without it, an offer's page is read for JSON-LD JobPosting

Templates take {path} of the item (or {term}), and |base64 or |url after
the path encodes it.

Requests say who they are (USER_AGENT), keep to what the portals allow
(robots.txt, their terms) and are spaced out per host. A list fetched for
one term serves the others for a few minutes (portals that can't be
searched give the same list for every term).

An offer, as every portal's is made to look (tidy()):

    {portal, key (the portal's id), url, title, company, cities, mode
    (remote, hybrid, office or ""), salary_min, salary_max, currency (a
    month's pay; None when not given), contracts (uop, b2b, other), level
    (intern, junior, mid, senior, lead or ""), skills, nice (skills nice
    to have), posted (epoch seconds or None), points (the [lat, lon] of
    its places, when the portal gives them), text (its full text, when
    the list gives it)}
"""

import base64
import datetime
import glob
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import config

log = logging.getLogger("portals")

USER_AGENT = ("CVAggregator/0.1 (private job search; "
              "+https://github.com/polon25/cv-aggregator)")
TIMEOUT = 30
MAX_PAGE = 8 * 1024 * 1024
# Work days and hours in a month, for pay given by the day or hour.
DAYS, HOURS = 21, 168
# Seconds a fetched list is kept for other terms.
CACHE_SECONDS = 600
MODES = ("remote", "hybrid", "office")
LEVEL_NAMES = ("intern", "junior", "mid", "senior", "lead")
CONTRACT_NAMES = ("uop", "b2b", "other")

_last = {}  # host -> time.monotonic() of the last request
_host_lock = threading.Lock()
_cache = {}  # (url, headers) -> (time.monotonic(), page)
_cache_lock = threading.Lock()


class PortalError(Exception):
    """A page that couldn't be had or read."""


# --- the portals there are -------------------------------------------------

PORTALS = {}  # id -> its description, in order


def load():
    """Reads the portals' files: portals/ beside the app, then
    config.PORTALS_DIR (whose files replace those of the same id);
    config.PORTALS, if set, keeps only the ones it names."""
    found = {}
    for directory in (os.path.join(config.BASE_DIR, "portals"),
                      config.PORTALS_DIR):
        if not directory:
            continue
        for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    portal = json.load(f)
                if not re.fullmatch(r"[a-z0-9_]+", portal.get("id", "")) or \
                        not (portal.get("parser") or portal.get("search") or
                             portal.get("import")):
                    raise ValueError("no id, or no parser, search or import")
            except (OSError, ValueError) as e:
                log.error("Portal %s left out: %s", path, e)
                continue
            found[portal["id"]] = portal
    if config.PORTALS:
        found = {k: v for k, v in found.items() if k in config.PORTALS}
    PORTALS.clear()
    # the default ones first, then by country and name
    PORTALS.update(sorted(found.items(), key=lambda kv: (
        not kv[1].get("default", True), kv[1].get("country") or "",
        (kv[1].get("name") or kv[0]).casefold())))
    log.info("Portals: %s", ", ".join(
        "%s%s" % (k, " (imported)" if not searchable(k) else
                  "" if v.get("default", True) else " (if chosen)")
        for k, v in PORTALS.items()))


def searchable(portal_id):
    portal = PORTALS.get(portal_id) or {}
    return bool(portal.get("parser") or portal.get("search"))


def defaults():
    """The portals searched for users who chose none."""
    return [k for k, v in PORTALS.items()
            if searchable(k) and v.get("default", True)]


def imported():
    """The portals the app can't search, whose pages users send."""
    return [k for k in PORTALS if not searchable(k)]


def identify(url):
    """(portal id, offer key or None, the offer's address) for an address
    on a portal users send pages of; a portal of none known is named by
    its host, and its key is the address without query."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    for portal_id, portal in PORTALS.items():
        spec = portal.get("import")
        if not spec or not any(host == h or host.endswith("." + h)
                               for h in spec.get("hosts") or []):
            continue
        m = re.search(spec.get("key") or "$^", url)
        key = m.group(1) if m else None
        address = template(spec["url"], {"key": key}) if key and \
            spec.get("url") else urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, parts.path, "", ""))
        return portal_id, key, address
    plain = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                     "", ""))
    site = re.sub(r"[^a-z0-9]+", "_", re.sub(r"^www\.", "", host)).strip("_")
    return site or "web", plain, plain


def name(portal_id):
    return (PORTALS.get(portal_id) or {}).get("name") or portal_id


# --- fetching ----------------------------------------------------------------

def get(url, headers=None, delay=None, cache=False):
    """The page at url, as text, after waiting for the host's turn (`delay`
    seconds after the last request; config.PORTAL_DELAY by default).
    With `cache`, a page fetched less than CACHE_SECONDS ago is reused."""
    headers = dict(headers or {})
    key = (url, tuple(sorted(headers.items())))
    if cache:
        with _cache_lock:
            kept = _cache.get(key)
        if kept and time.monotonic() - kept[0] < CACHE_SECONDS:
            return kept[1]
    host = urllib.parse.urlsplit(url).netloc
    with _host_lock:
        pause = config.PORTAL_DELAY if delay is None else delay
        wait = _last.get(host, 0) + pause - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()
    headers.setdefault("User-Agent", USER_AGENT)
    headers.setdefault("Accept-Language", "pl,en;q=0.8")
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_PAGE)
    except urllib.error.HTTPError as e:
        raise PortalError("HTTP %d: %s" % (e.code, url))
    except (urllib.error.URLError, OSError) as e:
        raise PortalError("%s: %s" % (url, e))
    log.debug("GET %s: %d bytes in %.1fs", url, len(body),
              time.monotonic() - started)
    page = body.decode("utf-8", "replace")
    if cache:
        with _cache_lock:
            _cache[key] = (time.monotonic(), page)
            for old in [k for k, (at, _) in _cache.items()
                        if time.monotonic() - at > CACHE_SECONDS]:
                del _cache[old]
    return page


# --- reading what pages carry -------------------------------------------

def script_json(page, script_id):
    """The JSON in <script id="...">."""
    m = re.search(r'<script id="%s"[^>]*>(.*?)</script>' % re.escape(
        script_id), page, re.S)
    try:
        return json.loads(m.group(1)) if m else None
    except ValueError:
        return None


def json_ld(page, kind):
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>'
                         r'(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == kind:
                return item
    return None


def parse(page, kind):
    """The JSON a page carries: kind json (the page is JSON), next_data
    (Next.js' __NEXT_DATA__ page props), json_ld (a JobPosting) or
    script:<id>."""
    if kind == "json":
        try:
            return json.loads(page)
        except ValueError:
            return None
    if kind == "next_data":
        return ((script_json(page, "__NEXT_DATA__") or {}).get("props")
                or {}).get("pageProps")
    if kind == "json_ld":
        return json_ld(page, "JobPosting")
    if kind.startswith("script:"):
        return script_json(page, kind[7:])
    raise PortalError("no such format: %s" % kind)


class _Text(HTMLParser):
    """HTML as plain text: blocks and list items on lines of their own."""
    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4",
              "h5", "tr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def text_of(markup):
    markup = markup if isinstance(markup, str) else ""
    if "&lt;" in markup and "<" not in markup:  # HTML escaped once more
        markup = html.unescape(markup)
    parser = _Text()
    parser.feed(markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def epoch(value):
    """Epoch seconds from milliseconds or seconds, or an ISO date or time
    in UTC ("2026-10-24T14:05:08.9675914Z", "2026-10-24"); None if it's
    none of these."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else value
    m = re.match(r"(\d{4}-\d\d-\d\d)(?:T(\d\d:\d\d:\d\d))?", str(value or ""))
    if not m:
        return None
    return datetime.datetime.strptime(
        m.group(1) + " " + (m.group(2) or "00:00:00"), "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=datetime.timezone.utc).timestamp()


def monthly(amount, unit):
    """A pay given by the hour, day, month or year, as a month's."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    unit = str(unit or "month").lower()
    factor = {"hour": HOURS, "day": DAYS, "year": 1 / 12}.get(unit, 1)
    return round(amount * factor)


def tidy(offer):
    """An offer as every portal's is made to look (see above): known
    values only, lists without repeats."""
    out = {"portal": offer["portal"], "key": str(offer["key"]),
           "url": offer["url"], "title": str(offer.get("title") or ""),
           "company": str(offer.get("company") or "")}
    out["cities"] = list(dict.fromkeys(
        str(c).strip() for c in offer.get("cities") or []
        if c and str(c).strip()))
    out["mode"] = offer.get("mode") if offer.get("mode") in MODES else ""
    out["salary_min"] = offer.get("salary_min")
    out["salary_max"] = offer.get("salary_max") or offer.get("salary_min")
    out["currency"] = offer.get("currency") if out["salary_max"] else None
    out["contracts"] = sorted({c for c in offer.get("contracts") or []
                               if c in CONTRACT_NAMES})
    out["level"] = offer.get("level") if offer.get("level") in LEVEL_NAMES \
        else ""
    for key in ("skills", "nice"):
        out[key] = list(dict.fromkeys(str(s) for s in offer.get(key) or []
                                      if s))
    out["posted"] = offer.get("posted")
    out["points"] = [list(p) for p in dict.fromkeys(
        tuple(p) for p in offer.get("points") or [])]
    if offer.get("text"):
        out["text"] = offer["text"][:config.OFFER_MAX_CHARS]
    return out


# --- described portals -----------------------------------------------------

def values(item, path):
    """The values at a path in an item: "a.b" (a dict's), "a[].b" (each of
    a list's); [] when there's nothing."""
    found = [item]
    for part in path.split("."):
        each = part.endswith("[]")
        part = part[:-2] if each else part
        nxt = []
        for value in found:
            if part:
                value = value.get(part) if isinstance(value, dict) else None
            if each and isinstance(value, list):
                nxt.extend(value)
            elif value is not None:
                nxt.append(value)
        found = nxt
    return [v for v in found if v is not None and v != ""]


def template(text, item):
    """{path} of the item put in; |base64 or |url after the path encodes."""
    def put(m):
        found = values(item, m.group(1))
        value = str(found[0]) if found else ""
        if m.group(2) == "base64":
            value = base64.b64encode(value.encode("utf-8")).decode("ascii")
        elif m.group(2) == "url":
            value = urllib.parse.quote(value, safe="")
        return value
    return re.sub(r"\{([^{}|]+)(?:\|(\w+))?\}", put, text)


def field(item, spec):
    """A field's values in an item, by its spec (see above)."""
    if isinstance(spec, str):
        found = values(item, spec)
    elif "value" in spec:
        value = spec["value"]
        found = value if isinstance(value, list) else [value]
    elif "template" in spec:
        found = [template(spec["template"], item)]
    elif "pairs" in spec:
        found = [list(p) for p in zip(*(values(item, path)
                                        for path in spec["pairs"]))]
    else:
        found = values(item, spec.get("path", ""))
    if isinstance(spec, dict) and spec.get("split"):
        found = [part.strip() for v in found
                 for part in str(v).split(spec["split"]) if part.strip()]
    if isinstance(spec, dict) and "map" in spec:
        table = {str(k).lower(): v for k, v in spec["map"].items()}
        found = [table[str(v).lower()] for v in found
                 if str(v).lower() in table]
    return [f for f in found if f is not None]


LISTS = ("cities", "contracts", "skills", "nice", "points")


def described(portal, item):
    """An offer from one item of a described portal's list; None without
    a key, an address or a title."""
    one = {}
    for name_, spec in portal["fields"].items():
        found = field(item, spec)
        one[name_] = found if name_ in LISTS else (found[0] if found
                                                   else None)
    period = one.pop("period", None)
    for key in ("salary_min", "salary_max"):
        one[key] = monthly(one.get(key), period)
    one["posted"] = epoch(one.get("posted"))
    if one.get("text"):
        one["text"] = text_of(str(one["text"])) if "<" in str(one["text"]) \
            else str(one["text"])
    one["portal"] = portal["id"]
    if not one.get("key") or not one.get("url") or not one.get("title"):
        return None
    return tidy(one)


def _search_described(portal, term):
    spec = portal["search"]
    context = {"term": term}
    params = {k: template(str(v), context)
              for k, v in (spec.get("params") or {}).items()}
    url = template(spec["url"], context)
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = parse(get(url, spec.get("headers"), portal.get("delay"),
                     cache=True), spec.get("format", "json"))
    items = values(data, spec["items"]) if data is not None else []
    offers = [o for o in (described(portal, i) for i in items
                          if isinstance(i, dict)) if o]
    if spec.get("filter"):
        words = _words(term)
        offers = [o for o in offers if words <= _words(" ".join(
            [o["title"], o["company"]] + o["skills"] + [o.get("text", "")]))]
    return offers


def _words(text):
    return set(re.findall(r"\w+", str(text).casefold()))


def _details_described(portal, offer):
    spec = portal["details"]
    page = get(template(spec["url"], offer), spec.get("headers"),
               portal.get("delay"))
    data = parse(page, spec.get("format", "json"))
    if data is None:
        return ""
    return "\n".join(text_of(str(v)) if "<" in str(v) else str(v)
                     for path in spec.get("text") or []
                     for v in values(data, path))


# --- searching ------------------------------------------------------------

def search(portal_id, term):
    """The offers a portal lists for a search term."""
    portal = PORTALS.get(portal_id)
    if not portal:
        raise PortalError("no portal %s" % portal_id)
    try:
        if portal.get("parser"):
            import sites
            return [tidy(o) for o in sites.search(portal, term)]
        return _search_described(portal, term)
    except PortalError:
        raise
    except Exception as e:  # a page or a file that changed its make
        raise PortalError("%s, %r: unreadable: %r" % (portal_id, term, e))


def details(offer):
    """The offer's full text, from the portal."""
    portal = PORTALS.get(offer["portal"]) or {}
    try:
        if portal.get("details"):
            text = _details_described(portal, offer)
        else:
            page = get(offer["url"], delay=portal.get("delay"))
            text = jobposting_text(page)
            if portal.get("parser"):
                import sites
                text = "\n".join(filter(None, [text, sites.details(
                    portal, offer, page)]))
    except PortalError:
        raise
    except Exception as e:
        raise PortalError("%s: unreadable: %r" % (offer["url"], e))
    text = text.strip()
    if not text:
        raise PortalError("no text on %s" % offer["url"])
    return text[:config.OFFER_MAX_CHARS]


def jobposting_text(page):
    """The text of the JSON-LD JobPosting a page has, "" if none."""
    posting = json_ld(page, "JobPosting")
    if not posting:
        return ""
    parts = [text_of(posting.get("description"))]
    for key in ("skills", "qualifications", "responsibilities",
                "experienceRequirements"):
        if isinstance(posting.get(key), str):
            parts.append(text_of(posting[key]))
    return "\n".join(p for p in parts if p)
