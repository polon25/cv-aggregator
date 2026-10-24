"""What the user looks for ("wishes") and how well an offer fits it,
worked out by the program alone, before any model reads an offer:

    terms()     what to search the portals for: the user's keywords and
                roles, or, while they've written none, what the analysis
                suggested
    reasons()   why an offer is no good whatever a model would think of
                it: the wrong place (farther than the user's distance from
                their cities), too little pay (or none given, if the
                user wants it given), the wrong contract or level, a word
                the user wants to avoid; rule_out() gives the first, and
                exception() the one that may be overlooked for an
                offer that looks exceptionally good
    prescore()  0..1, how promising an offer looks: its title against the
                roles sought, its skills against the candidate's

Offers that pass go to the model (agents.review_offer) best first, as many
a day as the user allows.
"""

import re

import candidate
import config
import portals

MODES = [("remote", "zdalnie"), ("hybrid", "hybrydowo"),
         ("office", "stacjonarnie")]
CONTRACTS = [("uop", "umowa o pracę"), ("b2b", "B2B"),
             ("other", "inna (zlecenie, dzieło, staż)")]
LEVELS = [("intern", "staż / praktyki"), ("junior", "junior"),
          ("mid", "mid"), ("senior", "senior"),
          ("lead", "lead / ekspert / kierownik")]
LISTS = ("roles", "keywords", "cities", "avoid")
CHOICES = {"modes": MODES, "contracts": CONTRACTS, "levels": LEVELS}
MAX_ENTRIES = 20
MAX_ENTRY = 80
# Rough złoty rates, for pay in other currencies against the user's least.
RATES = {"PLN": 1, "EUR": 4.3, "USD": 4.0, "GBP": 5.0, "CHF": 4.5}
# Kilometres from the user's cities within which offers do, unless they
# set their own; and the most they may.
DISTANCE = 30
MAX_DISTANCE = 300
# An offer breaking one criterion that may be overlooked (its level,
# contract, pay up to EXCEPTION_PAY below the least, or not saying what it
# pays) is kept anyway, as an exception, when it scores at least this...
EXCEPTION_PRESCORE = 0.7
EXCEPTION_PAY = 0.2
# ...shown once a model gave it at least this...
EXCEPTION_SCORE = 75
# ...and rated with at most this share of the day's number of ratings.
EXCEPTION_SHARE = 0.2
# Offers scoring less than this aren't kept for the user at all.
MIN_PRESCORE = 0.15


def empty():
    return {"roles": [], "keywords": [], "cities": [], "avoid": [],
            "modes": [], "contracts": [], "levels": [], "portals": [],
            "salary_min": None, "no_salary": True,
            "distance": DISTANCE, "exceptions": True}


def clean(raw):
    """The wishes from the form, fitted to their shape."""
    raw = raw if isinstance(raw, dict) else {}
    wishes = empty()
    for key in LISTS:
        wishes[key] = candidate.clean_value("list", raw.get(key),
                                            MAX_ENTRY)[:MAX_ENTRIES]
    for key, options in CHOICES.items():
        allowed = [k for k, _ in options]
        wishes[key] = [k for k in allowed if k in (raw.get(key) or [])]
    wishes["portals"] = [p for p in config.PORTALS
                         if p in (raw.get("portals") or [])]
    try:
        salary = int(float(str(raw.get("salary_min") or "").replace(
            " ", "").replace(",", ".")))
        wishes["salary_min"] = salary if 0 < salary < 10 ** 6 else None
    except ValueError:
        wishes["salary_min"] = None
    # offers that don't say what they pay are shown unless turned off
    wishes["no_salary"] = raw.get("no_salary") is not False
    wishes["exceptions"] = raw.get("exceptions") is not False
    try:
        distance = int(float(str(raw.get("distance")).replace(",", ".")))
        wishes["distance"] = max(0, min(MAX_DISTANCE, distance))
    except ValueError:
        wishes["distance"] = DISTANCE
    return wishes


def schema():
    """The choices for the page's form."""
    return {"modes": MODES, "contracts": CONTRACTS, "levels": LEVELS,
            "portals": [(p, portals.NAMES.get(p, p)) for p in config.PORTALS],
            "max": MAX_ENTRIES}


def describe(wishes, text=""):
    """The wishes as text for a model."""
    names = {k: dict(v) for k, v in CHOICES.items()}
    lines = []
    for key, label in (("roles", "Stanowiska / czym chce się zajmować"),
                       ("keywords", "Słowa kluczowe"),
                       ("cities", "Miejscowości (z okolicą)"),
                       ("avoid", "Unika")):
        if wishes.get(key):
            lines.append("%s: %s" % (label, ", ".join(wishes[key])))
    for key, label in (("modes", "Tryb pracy"), ("contracts", "Umowa"),
                       ("levels", "Poziom")):
        if wishes.get(key):
            lines.append("%s: %s" % (label, ", ".join(
                names[key][k] for k in wishes[key])))
    if wishes.get("cities") and wishes.get("distance"):
        lines.append("Odległość od tych miejscowości: do %d km"
                     % wishes["distance"])
    if wishes.get("salary_min"):
        lines.append("Wynagrodzenie co najmniej: %d PLN miesięcznie"
                     % wishes["salary_min"])
    if wishes.get("no_salary") is False:
        lines.append("Tylko oferty z podanym wynagrodzeniem")
    if text:
        lines.append(text)
    return "\n".join(lines)


def terms(wishes, analysis):
    """What to search the portals for, at most OFFERS_MAX_TERMS: the
    user's keywords and roles, by turns (the first of each first); with
    neither, the roles and keywords of the analysis' best career paths."""
    keywords, roles = wishes.get("keywords") or [], wishes.get("roles") or []
    found = [term for pair in zip(keywords, roles) for term in pair] + \
        keywords[len(roles):] + roles[len(keywords):]
    if not found and analysis:
        paths = sorted(analysis.get("career_paths") or [],
                       key=lambda p: {"wysokie": 0, "średnie": 1}.get(
                           p.get("fit"), 2))
        for path in paths[:2]:
            found += (path.get("roles") or [])[:2]
            found += (path.get("keywords") or [])[:2]
    out, seen = [], set()
    for term in found:
        term = re.sub(r"\s+", " ", term).strip()
        key = candidate.normalize(term)
        if term and key and key not in seen:
            seen.add(key)
            out.append(term)
    return out[:config.OFFERS_MAX_TERMS]


# --- judging an offer by what's known of it ------------------------------

CITY_NAMES = {"warsaw": "warszawa", "cracow": "krakow", "krakau": "krakow",
              "breslau": "wroclaw", "danzig": "gdansk", "posen": "poznan"}


def _city(city):
    """A city's name to compare: "Warsaw" and "Warszawa" are the same."""
    name = candidate.normalize(city)
    return CITY_NAMES.get(name, name)


def reasons(offer, wishes, near=None):
    """Why the offer doesn't do: [(a short reason in Polish, whether it
    may be overlooked for an exceptionally good offer)], empty when it
    does. `near`: whether any of the offer's places is near enough to the
    user's cities (None: not known, the cities' names are compared).
    Place, way of working and words to avoid are never overlooked; nor is
    pay more than EXCEPTION_PAY below the user's least."""
    found = []
    modes = wishes.get("modes") or []
    if modes and offer["mode"] and offer["mode"] not in modes:
        found.append(("tryb pracy", False))
    cities = {_city(c) for c in wishes.get("cities") or []}
    remote_ok = offer["mode"] == "remote" and (not modes or "remote" in modes)
    if cities and offer["cities"] and not remote_ok:
        if near is None:
            near = any(_city(c) in cities for c in offer["cities"])
        if not near:
            found.append(("miejscowość", False))
    if wishes.get("no_salary") is False and not offer.get("salary_max"):
        found.append(("brak widełek", True))
    least = wishes.get("salary_min")
    if least and offer.get("salary_max") and \
            offer.get("currency") in RATES:
        pay = offer["salary_max"] * RATES[offer["currency"]]
        if pay < least:
            found.append(("wynagrodzenie", pay >= least * (1 - EXCEPTION_PAY)))
    contracts = wishes.get("contracts") or []
    if contracts and offer["contracts"] and \
            not set(offer["contracts"]) & set(contracts):
        names = dict(CONTRACTS)
        found.append(("umowa: " + ", ".join(
            names.get(c, c) for c in offer["contracts"]), True))
    levels = wishes.get("levels") or []
    if levels and offer["level"] and offer["level"] not in levels:
        found.append(("poziom: " + dict(LEVELS).get(offer["level"],
                                                     offer["level"]), True))
    seen = candidate.normalize(offer["title"] + " " + offer["company"])
    for word in wishes.get("avoid") or []:
        word = candidate.normalize(word)
        if word and re.search(r"\b%s\b" % re.escape(word), seen):
            found.append(("unikane słowo", False))
            break
    return found


def rule_out(offer, wishes, near=None):
    """Why the offer doesn't do (the first reason), or None."""
    found = reasons(offer, wishes, near)
    return found[0][0] if found else None


def exception(offer, wishes, near, score):
    """The one criterion an offer breaks that may be overlooked, when the
    user allows exceptions and it looks exceptionally good (`score`, its
    prescore, at least EXCEPTION_PRESCORE); else None."""
    if wishes.get("exceptions") is False or score < EXCEPTION_PRESCORE:
        return None
    found = reasons(offer, wishes, near)
    if len(found) == 1 and found[0][1]:
        return found[0][0]
    return None


def skill_names(profile, wishes):
    """The candidate's skills and keywords, normalized, for prescore()."""
    names = set()
    for item in profile["sections"]["skills"]:
        names.add(candidate.normalize(item["name"]))
    for item in profile["sections"]["experience"] + \
            profile["sections"]["projects"]:
        names.update(candidate.normalize(s) for s in item.get("skills") or [])
    names.update(candidate.normalize(k) for k in wishes.get("keywords") or [])
    names.discard("")
    return names


def _role_match(title, role):
    """0..1: how much of a role sought the offer's title names (its words,
    whatever the inflection), or how alike they are."""
    words = candidate.stems(role)
    share = len(words & candidate.stems(title)) / len(words) if words else 0
    return max(share, candidate.similarity(title, role) or 0.0)


def prescore(offer, wishes, skills, analysis=None):
    """0..1: how promising the offer looks. The title against the roles
    sought (or, without any, the analysis' roles) counts most, then how
    many of the offer's skills the candidate has, then keywords in the
    title."""
    roles = list(wishes.get("roles") or [])
    if not roles and analysis:
        for path in analysis.get("career_paths") or []:
            roles += [path.get("title") or ""] + (path.get("roles") or [])
    title = offer["title"]
    role = max((_role_match(title, r) for r in roles if r), default=0.0)
    wanted = [s for s in offer["skills"] if candidate.normalize(s)]
    if wanted:
        had = sum(1 for s in wanted if candidate.normalize(s) in skills)
        skill = had / len(wanted)
    else:
        skill = 0.3  # nothing to go by
    title_words = set(candidate.normalize(title + " " + " ".join(
        offer["skills"])).split())
    keywords = [candidate.normalize(k) for k in wishes.get("keywords") or []]
    keyword = 1.0 if any(k and set(k.split()) <= title_words
                         for k in keywords) else 0.0
    if not roles:
        return round(0.6 * skill + 0.4 * keyword, 3)
    return round(0.45 * role + 0.35 * skill + 0.2 * keyword, 3)


def same_offer(a, b):
    """The same job posted on two portals (or twice on one)."""
    return candidate.normalize(a["company"]) == candidate.normalize(
        b["company"]) and (candidate.similarity(a["title"], b["title"])
                           or 0) >= 0.9
