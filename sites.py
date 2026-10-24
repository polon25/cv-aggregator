"""Portals that need code to be read (portals.py describes the rest in
JSON). A portal's file names its parser ("parser") and gives it what it
takes; search(portal, term) gives its offers for a term, details(portal,
offer, page) what an offer's page says beyond its JSON-LD JobPosting.

    justjoin     justjoin.it and rocketjobs.pl (one platform): a list page
                 carries its 50 offers' data in Next.js' flight data; an
                 offer's page has JSON-LD. Takes "list_url" (searched with
                 ?keyword=) and "offer_url" (an offer's slug is added).
    nofluffjobs  nofluffjobs.com: a list page's state (serverApp-state,
                 its quotes coded) holds 20 offers; so does an offer's
                 page its requirements and tasks. Takes "list_url"
                 (searched with ?criteria=keyword=) and "offer_url".
    bulldogjob   bulldogjob.pl: a list page's __NEXT_DATA__ holds 50
                 offers; it's searched by skill only (a term that isn't
                 one finds nothing). Takes "list_url" (the skill is added)
                 and "offer_url".
"""

import json
import re
import urllib.parse

import portals

LEVELS = {"trainee": "intern", "intern": "intern", "internship": "intern",
          "junior": "junior", "mid": "mid", "medium": "mid", "regular": "mid",
          "senior": "senior", "expert": "lead", "lead": "lead",
          "c_level": "lead", "manager": "lead", "head": "lead"}
CONTRACTS = {"permanent": "uop", "employment": "uop", "b2b": "b2b"}


def _get(portal, url):
    return portals.get(url, delay=portal.get("delay"))


# --- justjoin.it, rocketjobs.pl ------------------------------------------

def _flight_offers(page):
    """The "offers" array in Next.js' flight data (self.__next_f.push
    chunks)."""
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


def _justjoin_offer(portal, offer):
    pay = [e for e in offer.get("employmentTypes") or []
           if e.get("currencySource") == "original" and e.get("from")]
    first = pay[0] if pay else {}
    places = [offer] + (offer.get("multilocation") or [])
    return {
        "portal": portal["id"], "key": offer.get("guid") or offer["slug"],
        "url": portal["offer_url"] + offer["slug"],
        "title": offer.get("title"), "company": offer.get("companyName"),
        "cities": [p.get("city") for p in places],
        "mode": offer.get("workplaceType"),
        "salary_min": portals.monthly(first.get("from"), first.get("unit")),
        "salary_max": portals.monthly(first.get("to") or first.get("from"),
                                      first.get("unit")),
        "currency": first.get("currency"),
        "contracts": [CONTRACTS.get(e.get("type"), "other")
                      for e in offer.get("employmentTypes") or []],
        "level": LEVELS.get(str(offer.get("experienceLevel")).lower()),
        "skills": offer.get("requiredSkills"),
        "nice": offer.get("niceToHaveSkills"),
        "posted": portals.epoch(offer.get("publishedAt")),
        "points": [[p["latitude"], p["longitude"]] for p in places
                   if isinstance(p.get("latitude"), (int, float)) and
                   isinstance(p.get("longitude"), (int, float))],
    }


def _justjoin(portal, term):
    page = _get(portal, portal["list_url"] + "?" + urllib.parse.urlencode(
        {"keyword": term}))
    return [_justjoin_offer(portal, o) for o in _flight_offers(page)
            if o.get("slug")]


# --- nofluffjobs.com ------------------------------------------------------

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


def _nofluffjobs_offer(portal, p):
    location = p.get("location") or {}
    places = [pl for pl in location.get("places") or []
              if pl.get("city") and pl.get("city") != "Remote"]
    salary = p.get("salary") or {}
    tiles = (p.get("tiles") or {}).get("values") or []
    return {
        "portal": portal["id"], "key": p.get("id") or p["url"],
        "url": portal["offer_url"] + p["url"],
        "title": p.get("title"), "company": p.get("name"),
        "cities": [pl["city"] for pl in places],
        "mode": "remote" if location.get("fullyRemote") else
        location.get("type"),
        "salary_min": portals.monthly(salary.get("from"),
                                      salary.get("period")),
        "salary_max": portals.monthly(salary.get("to") or salary.get("from"),
                                      salary.get("period")),
        "currency": salary.get("currency"),
        "contracts": [CONTRACTS.get(salary.get("type"), "other")]
        if salary.get("type") else [],
        "level": next((LEVELS[s.lower()] for s in p.get("seniority") or []
                       if s.lower() in LEVELS), ""),
        "skills": [t["value"] for t in tiles if t.get("type") == "requirement"],
        "posted": portals.epoch(p.get("renewed") or p.get("posted")),
        "points": [[pl["geoLocation"]["latitude"],
                    pl["geoLocation"]["longitude"]] for pl in places
                   if isinstance(pl.get("geoLocation"), dict) and
                   isinstance(pl["geoLocation"].get("latitude"),
                              (int, float))],
    }


def _nofluffjobs(portal, term):
    page = _get(portal, portal["list_url"] + "?" + urllib.parse.urlencode(
        {"criteria": "keyword=" + term}))
    for value in _nfj_state(page).values():
        if isinstance(value, dict) and "postings" in value:
            return [_nofluffjobs_offer(portal, p) for p in value["postings"]
                    if p.get("url")]
    return []


def _nofluffjobs_details(portal, offer, page):
    for value in _nfj_state(page).values():
        if not isinstance(value, dict) or "requirements" not in value:
            continue
        parts = []
        req = value.get("requirements") or {}
        musts = [m.get("value") for m in req.get("musts") or []]
        nices = [m.get("value") for m in req.get("nices") or []]
        if any(musts):
            parts.append("Wymagane: " + ", ".join(filter(None, musts)))
        if any(nices):
            parts.append("Mile widziane: " + ", ".join(filter(None, nices)))
        parts.append(portals.text_of(req.get("description")))
        parts.append(portals.text_of((value.get("details") or {}).get(
            "description")))
        specs = value.get("specs")
        tasks = specs.get("dailyTasks") if isinstance(specs, dict) else None
        if isinstance(tasks, list):
            parts.append("Codzienne zadania:\n" + "\n".join(
                "- " + portals.text_of(t) for t in tasks
                if isinstance(t, str)))
        return "\n".join(p for p in parts if p)
    return ""


# --- bulldogjob.pl ---------------------------------------------------------

def _bulldogjob_offer(portal, job):
    pay = job.get("denominatedSalaryLong") or {}
    numbers = [int(n.replace(" ", "")) for n in
               re.findall(r"\d[\d ]*", pay.get("money") or "")] \
        if not pay.get("hidden") else []
    remote = job.get("remote") or (job.get("environment") or {}).get(
        "remotePossible") == 100
    return {
        "portal": portal["id"], "key": job["id"],
        "url": portal["offer_url"] + job["id"],
        "title": job.get("position"),
        "company": (job.get("company") or {}).get("name"),
        "cities": (job.get("city") or "").split(","),
        "mode": "remote" if remote else "",
        "salary_min": numbers[0] if numbers else None,
        "salary_max": numbers[-1] if numbers else None,
        "currency": pay.get("currency") if numbers else None,
        "contracts": [c for c, on in (("uop", job.get("contractEmployment")),
                                      ("b2b", job.get("contractB2b")),
                                      ("other", job.get("contractOther")))
                      if on],
        "level": LEVELS.get(str(job.get("experienceLevel")).lower()),
        "skills": job.get("technologyTags"),
    }


def _bulldogjob(portal, term):
    page = _get(portal, portal["list_url"] + urllib.parse.quote(term))
    data = portals.parse(page, "next_data") or {}
    # a skill it doesn't know isn't a filter: all its jobs come back
    filters = (data.get("slugState") or {}).get("filters") or {}
    if not filters.get("skills"):
        return []
    return [_bulldogjob_offer(portal, j) for j in data.get("jobs") or []
            if j.get("id")]


# --- the parsers ---------------------------------------------------------

PARSERS = {
    "justjoin": (_justjoin, None),
    "nofluffjobs": (_nofluffjobs, _nofluffjobs_details),
    "bulldogjob": (_bulldogjob, None),
}


def search(portal, term):
    return PARSERS[portal["parser"]][0](portal, term)


def details(portal, offer, page):
    extra = PARSERS[portal["parser"]][1]
    return extra(portal, offer, page) if extra else ""
