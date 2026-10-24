"""What the job market asks of the candidate, counted by the program from
the offers kept for them (the ones that passed their wishes): demand()
gives the skills offers ask for most that the candidate lacks, and the
ones they have that are asked for most, each offer weighing as much as it
fits (its score, less what unmet wishes cost; its prescore until it's
rated). agents.advise() turns that into advice.
"""

import time

import candidate
import config
import db
import matching

# Skills an offer only wishes for weigh this much of the ones it requires.
NICE = 0.5
# Offers weighing less than this aren't counted.
LEAST_WEIGHT = 0.2
# A skill lacking is listed when this many offers ask for it at least.
LEAST_OFFERS = 2
LISTED = 15
EXAMPLES = 3
# Tags portals put on offers that name no skill.
GENERIC = {"test", "testing", "tests", "oprogramowanie", "oprogramowania",
           "software", "development", "engineering", "inzynieria", "it",
           "programming", "programowanie", "communication", "teamwork",
           "komunikatywnosc", "praca w zespole", "team player", "analytical",
           "problem solving", "backend", "frontend", "fullstack", "other",
           "inne", "agile"}


def _weight(match):
    if match["score"] is not None:
        return max(0, match["score"] - match["penalty"]) / 100
    return max(0.0, match["prescore"]) * 0.8


def _has(key, own, names, languages):
    if key in own or candidate.name_key("languages", key) in languages:
        return True
    return any((candidate.similarity(key, name) or 0) >= 0.9
               for name in names)


def _general(keys):
    """{key: the most general key among them it's a form of}: a key whose
    words are all in another's stands for both ("ni teststand" →
    "teststand", "linux os" → "linux")."""
    out = {}
    for key in keys:
        words = set(key.split())
        broader = [k for k in keys if k != key and set(k.split()) < words]
        out[key] = min(broader, key=len) if broader else key
    return out


def demand(conn, user_id, profile, wishes):
    """{offers: how many were counted, missing: [{name, offers, share,
    weight, examples}], have: [same]}, the most wanted first."""
    since = time.time() - 2 * config.OFFER_DAYS * 86400
    own = matching.skill_names(profile, wishes)
    names = [i["name"] for i in profile["sections"]["skills"]]
    languages = {candidate.name_key("languages", i["name"])
                 for i in profile["sections"]["languages"]}
    counted, asks = 0, []
    for m in db.user_matches(conn, user_id):
        weight = _weight(m)
        if m["status"] == "rejected" or m["last_seen"] < since or \
                weight < LEAST_WEIGHT:
            continue
        counted += 1
        for name, share in [(s, 1.0) for s in m["skills"]] + \
                [(s, NICE) for s in m["nice"]]:
            key = candidate.normalize(name)
            if key and key not in GENERIC:
                asks.append((key, name, share, weight, m))
    general = _general({key for key, *_ in asks})
    skills = {}
    for key, name, share, weight, m in asks:
        entry = skills.setdefault(general[key], {
            "names": {}, "offers": set(), "weight": 0.0, "examples": []})
        if m["offer_id"] in entry["offers"]:
            continue
        entry["names"][name] = entry["names"].get(name, 0) + 1
        entry["offers"].add(m["offer_id"])
        entry["weight"] += weight * share
        entry["examples"].append((weight, m["title"], m["company"]))
    missing, have = [], []
    for key, entry in skills.items():
        item = {
            "name": max(entry["names"], key=entry["names"].get),
            "offers": len(entry["offers"]),
            "share": round(len(entry["offers"]) / counted, 2),
            "weight": round(entry["weight"], 2),
            "examples": ["%s (%s)" % (title, company) for _, title, company
                         in sorted(entry["examples"],
                                   reverse=True)[:EXAMPLES]],
        }
        if _has(key, own, names, languages):
            if item["offers"] >= LEAST_OFFERS:
                have.append(item)
        elif item["offers"] >= LEAST_OFFERS:
            missing.append(item)
    order = lambda i: (-i["weight"], -i["offers"])
    return {"offers": counted, "missing": sorted(missing, key=order)[:LISTED],
            "have": sorted(have, key=order)[:LISTED]}


def cons(conn, user_id, least=45, most=40):
    """What the model found missing in the best offers kept for the user
    (the score, less unmet wishes, at least `least`), best first."""
    found = []
    for m in sorted(db.user_matches(conn, user_id),
                    key=lambda m: -(m["score"] or 0) + m["penalty"]):
        if m["review"] and m["status"] != "rejected" and \
                m["score"] - m["penalty"] >= least:
            found += ["%s: %s" % (m["title"], c) for c in m["review"]["cons"]]
    return found[:most]
