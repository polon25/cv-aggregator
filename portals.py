"""Job portals: search(portal, term) reads one portal's list of offers for a
search term, details(offer) the full text of one offer. Standard library
only; pages are read the way a browser gets them, with no private API:

    justjoin     justjoin.it, IT jobs: a list page carries its 50 offers'
                 data (Next.js' flight data), an offer's page its text
                 (JSON-LD JobPosting)
    rocketjobs   rocketjobs.pl, the same platform for jobs outside IT
    nofluffjobs  nofluffjobs.com: a list page's state holds 20 offers a
                 page, an offer's page the offer
    bulldogjob   bulldogjob.pl: a list page's __NEXT_DATA__ holds 50
                 offers; searched by skill (it has no text search for
                 pages), an offer's page has JSON-LD JobPosting

Requests say who they are (USER_AGENT), keep to what the portals'
robots.txt allows (no /api/ paths) and are spaced out: at least
config.PORTAL_DELAY seconds between two to the same host.

An offer, as every portal's is made to look:

    {portal, key (the portal's id), url, title, company, cities, mode
    (remote, hybrid, office or ""), salary_min, salary_max, currency (a
    month's pay; None when not given), contracts (uop, b2b, other), level
    (intern, junior, mid, senior, lead or ""), skills, nice (skills nice
    to have), posted (epoch seconds or None)}
"""

import datetime
import html
import json
import logging
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
MAX_PAGE = 4 * 1024 * 1024
NAMES = {"justjoin": "JustJoin.it", "rocketjobs": "RocketJobs",
         "nofluffjobs": "No Fluff Jobs", "bulldogjob": "Bulldogjob"}
# Work days and hours in a month, for pay given by the day or hour.
DAYS, HOURS = 21, 168

_last = {}  # host -> time.monotonic() of the last request
_host_lock = threading.Lock()


class PortalError(Exception):
    """A page that couldn't be had or read."""


def _get(url):
    """The page at url, as text, after waiting for the host's turn."""
    host = urllib.parse.urlsplit(url).netloc
    with _host_lock:
        wait = _last.get(host, 0) + config.PORTAL_DELAY - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Language": "pl,en;q=0.8"})
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
    return body.decode("utf-8", "replace")


# --- reading what pages carry -------------------------------------------

def _flight_offers(page):
    """The offers of a justjoin/rocketjobs list page: an "offers" array in
    Next.js' flight data (self.__next_f.push chunks)."""
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page,
                            re.S):
        try:
            text = json.loads('"' + chunk + '"')
        except ValueError:
            continue
        start = text.find('{"offers":[')
        if start >= 0:
            try:
                return json.JSONDecoder().raw_decode(text, start)[0]["offers"]
            except (ValueError, KeyError):
                pass
    return []


def _nfj_state(page):
    m = re.search(r'<script id="serverApp-state"[^>]*>(.*?)</script>', page,
                  re.S)
    if not m:
        return {}
    text = m.group(1)
    for code, char in (("&q;", '"'), ("&s;", "'"), ("&l;", "<"),
                       ("&g;", ">"), ("&a;", "&")):
        text = text.replace(code, char)
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page,
                  re.S)
    try:
        return json.loads(m.group(1))["props"]["pageProps"] if m else {}
    except (ValueError, KeyError):
        return {}


def _json_ld(page, kind):
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>'
                         r'(.*?)</script>', page, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == kind:
            return data
    return None


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
    markup = markup or ""
    if "&lt;" in markup and "<" not in markup:  # HTML escaped once more
        markup = html.unescape(markup)
    parser = _Text()
    parser.feed(markup)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _epoch(value):
    """Epoch seconds from milliseconds or an ISO time in UTC
    ("2026-10-24T14:05:08.9675914Z"); None if it's neither."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    m = re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", str(value or ""))
    if not m:
        return None
    return datetime.datetime.strptime(m.group(0), "%Y-%m-%dT%H:%M:%S") \
        .replace(tzinfo=datetime.timezone.utc).timestamp()


def _monthly(amount, unit):
    """A pay given by the hour, day, month or year, as a month's."""
    if amount is None:
        return None
    unit = str(unit or "month").lower()
    factor = {"hour": HOURS, "day": DAYS, "year": 1 / 12}.get(unit, 1)
    return round(float(amount) * factor)


LEVELS = {"trainee": "intern", "intern": "intern", "internship": "intern",
          "junior": "junior", "mid": "mid", "medium": "mid", "regular": "mid",
          "senior": "senior", "expert": "lead", "lead": "lead",
          "c_level": "lead", "manager": "lead", "head": "lead"}
CONTRACTS = {"permanent": "uop", "employment": "uop", "b2b": "b2b"}


# --- the portals ---------------------------------------------------------

def _justjoin(offer, site):
    pay = [e for e in offer.get("employmentTypes") or []
           if e.get("currencySource") == "original" and e.get("from")]
    first = pay[0] if pay else {}
    cities = [offer.get("city")] + [m.get("city") for m in
                                    offer.get("multilocation") or []]
    url = ("https://justjoin.it/job-offer/" if site == "justjoin" else
           "https://rocketjobs.pl/oferta-pracy/") + offer["slug"]
    return {
        "portal": site, "key": offer.get("guid") or offer["slug"],
        "url": url, "title": offer.get("title") or "",
        "company": offer.get("companyName") or "",
        "cities": list(dict.fromkeys(c for c in cities if c)),
        "mode": offer.get("workplaceType") if offer.get("workplaceType") in (
            "remote", "hybrid", "office") else "",
        "salary_min": _monthly(first.get("from"), first.get("unit")),
        "salary_max": _monthly(first.get("to") or first.get("from"),
                               first.get("unit")),
        "currency": first.get("currency"),
        "contracts": sorted({CONTRACTS.get(e.get("type"), "other")
                             for e in offer.get("employmentTypes") or []}),
        "level": LEVELS.get(str(offer.get("experienceLevel")).lower(), ""),
        "skills": offer.get("requiredSkills") or [],
        "nice": offer.get("niceToHaveSkills") or [],
        "posted": _epoch(offer.get("publishedAt")),
    }


def _search_justjoin(term, site="justjoin"):
    base = "https://justjoin.it/job-offers/all-locations" \
        if site == "justjoin" else \
        "https://rocketjobs.pl/oferty-pracy/wszystkie-lokalizacje"
    page = _get(base + "?" + urllib.parse.urlencode({"keyword": term}))
    return [_justjoin(o, site) for o in _flight_offers(page) if o.get("slug")]


def _nofluffjobs(p):
    location = p.get("location") or {}
    places = [pl.get("city") for pl in location.get("places") or []
              if pl.get("city") and pl.get("city") != "Remote"]
    salary = p.get("salary") or {}
    tiles = (p.get("tiles") or {}).get("values") or []
    mode = "remote" if location.get("fullyRemote") else \
        location.get("type") if location.get("type") in (
            "hybrid", "office", "remote") else ""
    return {
        "portal": "nofluffjobs", "key": p.get("id") or p["url"],
        "url": "https://nofluffjobs.com/pl/job/" + p["url"],
        "title": p.get("title") or "", "company": p.get("name") or "",
        "cities": list(dict.fromkeys(places)), "mode": mode,
        "salary_min": _monthly(salary.get("from"), salary.get("period")),
        "salary_max": _monthly(salary.get("to") or salary.get("from"),
                               salary.get("period")),
        "currency": salary.get("currency"),
        "contracts": [CONTRACTS.get(salary.get("type"), "other")]
        if salary.get("type") else [],
        "level": next((LEVELS[s.lower()] for s in p.get("seniority") or []
                       if s.lower() in LEVELS), ""),
        "skills": [t["value"] for t in tiles if t.get("type") == "requirement"],
        "nice": [],
        "posted": _epoch(p.get("renewed") or p.get("posted")),
    }


def _search_nofluffjobs(term):
    page = _get("https://nofluffjobs.com/pl/?" + urllib.parse.urlencode(
        {"criteria": "keyword=" + term}))
    for value in _nfj_state(page).values():
        if isinstance(value, dict) and "postings" in value:
            return [_nofluffjobs(p) for p in value["postings"] if p.get("url")]
    return []


def _bulldogjob(job):
    pay = job.get("denominatedSalaryLong") or {}
    numbers = [int(n.replace(" ", "")) for n in
               re.findall(r"\d[\d ]*", pay.get("money") or "")] \
        if not pay.get("hidden") else []
    contracts = [c for c, on in (("uop", job.get("contractEmployment")),
                                 ("b2b", job.get("contractB2b")),
                                 ("other", job.get("contractOther"))) if on]
    remote = job.get("remote") or (job.get("environment") or {}).get(
        "remotePossible") == 100
    return {
        "portal": "bulldogjob", "key": job["id"],
        "url": "https://bulldogjob.pl/companies/jobs/" + job["id"],
        "title": job.get("position") or "",
        "company": (job.get("company") or {}).get("name") or "",
        "cities": [c.strip() for c in (job.get("city") or "").split(",")
                   if c.strip()],
        "mode": "remote" if remote else "",
        "salary_min": numbers[0] if numbers else None,
        "salary_max": numbers[-1] if numbers else None,
        "currency": pay.get("currency") if numbers else None,
        "contracts": contracts,
        "level": LEVELS.get(str(job.get("experienceLevel")).lower(), ""),
        "skills": job.get("technologyTags") or [], "nice": [],
        "posted": None,
    }


def _search_bulldogjob(term):
    page = _get("https://bulldogjob.pl/companies/jobs/s/skills," +
                urllib.parse.quote(term))
    data = _next_data(page)
    # a skill it doesn't know isn't a filter: all its jobs come back
    filters = (data.get("slugState") or {}).get("filters") or {}
    if not filters.get("skills"):
        return []
    return [_bulldogjob(j) for j in data.get("jobs") or [] if j.get("id")]


SEARCH = {
    "justjoin": _search_justjoin,
    "rocketjobs": lambda term: _search_justjoin(term, "rocketjobs"),
    "nofluffjobs": _search_nofluffjobs,
    "bulldogjob": _search_bulldogjob,
}


def search(portal, term):
    """The offers a portal lists for a search term."""
    try:
        return SEARCH[portal](term)
    except PortalError:
        raise
    except Exception as e:  # a page that changed its make
        raise PortalError("%s, %r: unreadable page: %s" % (portal, term, e))


def details(offer):
    """The offer's full text, from its page."""
    page = _get(offer["url"])
    parts = []
    posting = _json_ld(page, "JobPosting")
    if posting:
        parts.append(text_of(posting.get("description")))
        for key in ("skills", "qualifications", "responsibilities",
                    "experienceRequirements"):
            if isinstance(posting.get(key), str):
                parts.append(text_of(posting[key]))
    if offer["portal"] == "nofluffjobs":
        for value in _nfj_state(page).values():
            if isinstance(value, dict) and "requirements" in value:
                req = value.get("requirements") or {}
                musts = [m.get("value") for m in req.get("musts") or []]
                nices = [m.get("value") for m in req.get("nices") or []]
                if any(musts):
                    parts.append("Wymagane: " + ", ".join(filter(None,
                                                                 musts)))
                if nices:
                    parts.append("Mile widziane: " + ", ".join(
                        filter(None, nices)))
                parts.append(text_of(req.get("description")))
                parts.append(text_of((value.get("details") or {}).get(
                    "description")))
                specs = value.get("specs")
                tasks = specs.get("dailyTasks") if isinstance(specs, dict) \
                    else None
                if isinstance(tasks, list):
                    parts.append("Codzienne zadania:\n" + "\n".join(
                        "- " + text_of(t) for t in tasks if isinstance(t, str)))
                break
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        raise PortalError("no text on %s" % offer["url"])
    return text[:config.OFFER_MAX_CHARS]
