"""The candidate's profile: what all their CVs say, merged, with what the
user changed by hand on top.

A model reads each CV once (agents.read_cv) into an "extraction": personal
details and lists of items in SECTIONS (jobs, schools, skills...).
clean_extraction() fits what the model wrote to the schema. build() merges
the extractions of all CVs, newest first: items of different CVs that
describe the same thing (the same job: the same employer and dates, even
when one CV is Polish and another English) become one, whose fields come
from the newest CV that has them; lists of achievements are joined.

What the user does is kept apart, in "overrides", so that merging can be
done again whenever a CV comes or goes:

    personal  {field: value} the user set ("" clears a field on purpose)
    items     {section: [item]}: items the user added or edited. An edited
              one names the CVs' items it replaces ("replaces": refs); it
              takes their place whole, as the user wrote it. Items of
              CVs added later that match it join it too.
    removed   {section: [{id, refs, item}]}: what the user removed: those
              CVs' items, and ones of later CVs that match it, stay out
              until it's restored.

A ref, "<cv id>:<index>", names one item of one CV's extraction. stats()
works out what can be counted (years of experience, the highest degree)
and text() writes the profile as plain text for a model.
"""

import datetime
import difflib
import hashlib
import json
import re
import secrets
import unicodedata

# --- the schema ------------------------------------------------------------
# A field: (key, label, kind, longest), kinds: text, textarea, date ("YYYY"
# or "YYYY-MM"), bool, list (of short texts). An item's first field is the
# one it can't do without.

PERSONAL = [
    ("name", "Imię i nazwisko", "text", 100),
    ("headline", "Nagłówek zawodowy", "text", 200),
    ("email", "E-mail", "text", 120),
    ("phone", "Telefon", "text", 40),
    ("location", "Miejscowość", "text", 100),
    ("links", "Linki (LinkedIn, GitHub, strona)", "list", 200),
    ("summary", "Podsumowanie z CV", "textarea", 3000),
]

SECTIONS = [
    ("experience", "Doświadczenie zawodowe", [
        ("title", "Stanowisko", "text", 150),
        ("organization", "Pracodawca", "text", 150),
        ("location", "Miejsce", "text", 100),
        ("start", "Od", "date", 7),
        ("end", "Do", "date", 7),
        ("current", "Nadal tu pracuję", "bool", 1),
        ("description", "Opis", "textarea", 3000),
        ("highlights", "Zadania i osiągnięcia", "list", 400),
        ("skills", "Technologie i narzędzia", "list", 60),
    ]),
    ("education", "Wykształcenie", [
        ("institution", "Uczelnia / szkoła", "text", 150),
        ("degree", "Stopień / tytuł", "text", 100),
        ("field", "Kierunek i specjalność", "text", 200),
        ("start", "Od", "date", 7),
        ("end", "Do", "date", 7),
        ("current", "Nadal się uczę", "bool", 1),
        ("thesis", "Temat pracy dyplomowej", "text", 300),
        ("description", "Opis", "textarea", 2000),
    ]),
    ("skills", "Umiejętności", [
        ("name", "Umiejętność", "text", 80),
        ("category", "Kategoria", "text", 80),
        ("level", "Poziom", "text", 60),
    ]),
    ("languages", "Języki", [
        ("name", "Język", "text", 50),
        ("level", "Poziom", "text", 60),
    ]),
    ("certificates", "Kursy i certyfikaty", [
        ("name", "Nazwa", "text", 200),
        ("issuer", "Wystawca / organizator", "text", 150),
        ("date", "Data", "date", 7),
        ("description", "Opis", "textarea", 1000),
    ]),
    ("projects", "Projekty", [
        ("name", "Nazwa", "text", 150),
        ("role", "Rola", "text", 150),
        ("start", "Od", "date", 7),
        ("end", "Do", "date", 7),
        ("description", "Opis", "textarea", 2000),
        ("skills", "Technologie i narzędzia", "list", 60),
        ("url", "Link", "text", 300),
    ]),
    ("publications", "Publikacje", [
        ("title", "Tytuł", "text", 400),
        ("authors", "Autorzy", "text", 400),
        ("venue", "Czasopismo / konferencja", "text", 200),
        ("date", "Rok", "date", 7),
        ("url", "Link / DOI", "text", 300),
    ]),
    ("awards", "Nagrody i wyróżnienia", [
        ("name", "Nazwa", "text", 200),
        ("issuer", "Przyznający", "text", 150),
        ("date", "Data", "date", 7),
        ("description", "Opis", "textarea", 1000),
    ]),
    ("interests", "Zainteresowania", [
        ("name", "Zainteresowanie", "text", 100),
    ]),
]

FIELDS = {name: fields for name, _, fields in SECTIONS}
FIELDS_KEYS = {name: [f[0] for f in spec] for name, spec in FIELDS.items()}
TITLES = {name: title for name, title, _ in SECTIONS}
MAX_ITEMS = 200       # in one section
MAX_LIST = 40         # entries of a list field
MAX_ABOUT = 6000      # the user's own description, and what they look for


def schema():
    """The schema for the page's forms."""
    def fields(spec):
        return [{"key": k, "label": label, "kind": kind, "max": longest}
                for k, label, kind, longest in spec]
    return {"personal": fields(PERSONAL),
            "sections": [{"key": name, "title": title, "fields": fields(spec)}
                         for name, title, spec in SECTIONS]}


# --- cleaning what a model or the user wrote --------------------------------

def clean_date(value):
    """"YYYY-MM" or "YYYY" from what a CV or a model writes ("03.2019",
    "2019-3", "2019-03-01", "marzec 2019" isn't understood: ""). Years
    before 1950 or after next year are rejected."""
    value = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})(?:[-./](\d{1,2})(?:[-./]\d{1,2})?)?", value) \
        or re.fullmatch(r"(?:\d{1,2}[-./])?(\d{1,2})[-./](\d{4})", value)
    if not m:
        return ""
    if len(m.group(1)) == 4:
        year, month = int(m.group(1)), m.group(2)
    else:
        year, month = int(m.group(2)), m.group(1)
    if not 1950 <= year <= datetime.date.today().year + 1:
        return ""
    if month and 1 <= int(month) <= 12:
        return "%04d-%02d" % (year, int(month))
    return "%04d" % year


def clean_text(value, longest, multiline=False):
    if isinstance(value, (list, dict)) or value is None:
        return ""
    text = str(value).strip()
    if multiline:
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = " ".join(text.split())
    return text[:longest]


def clean_value(kind, value, longest):
    if kind == "date":
        return clean_date(value)
    if kind == "bool":
        return value is True or str(value).strip().lower() in (
            "1", "true", "yes", "tak", "on")
    if kind == "list":
        if isinstance(value, str):
            value = value.split("\n")
        if not isinstance(value, list):
            return []
        out = []
        for entry in value:
            entry = clean_text(entry, longest).lstrip("•-–○◦·* ").strip()
            if entry and entry not in out:
                out.append(entry)
        return out[:MAX_LIST]
    return clean_text(value, longest, kind == "textarea")


def clean_item(section, raw):
    """An item of a section with just the schema's fields, or None when
    its first field is empty."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("current"), str) and \
            raw.get("current").strip().lower() in ("obecnie", "present"):
        raw = dict(raw, current=True)
    item = {}
    for key, _, kind, longest in FIELDS[section]:
        item[key] = clean_value(kind, raw.get(key), longest)
    first = FIELDS[section][0][0]
    if not item[first]:
        return None
    # a description that's only one of the achievements again
    if item.get("description") and any(same_sentence(item["description"], h)
                                       for h in item.get("highlights", [])):
        item["description"] = ""
    if "end" in item:
        if str(raw.get("end") or "").strip().lower() in (
                "obecnie", "present", "now", "teraz", "nadal"):
            item["current"] = True
        if item.get("current"):
            item["end"] = ""
        if item.get("start") and item["end"] and item["end"] < item["start"]:
            item["start"], item["end"] = item["end"], item["start"]
    return item


def clean_personal(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {key: clean_value(kind, raw.get(key), longest)
            for key, _, kind, longest in PERSONAL}


def clean_extraction(raw):
    """What a model read from a CV, fitted to the schema:
    {personal, <section>: [items]}. Raises ValueError when nothing a CV
    would hold is there."""
    out = {"personal": clean_personal(raw.get("personal"))}
    found = 0
    for section in FIELDS:
        items = raw.get(section)
        if section == "interests" and isinstance(items, list):
            items = [{"name": i} if isinstance(i, str) else i for i in items]
        clean = []
        for entry in items if isinstance(items, list) else []:
            item = clean_item(section, entry)
            if item and item not in clean:
                clean.append(item)
        out[section] = clean[:MAX_ITEMS]
        found += len(clean)
    if not found and not out["personal"]["name"]:
        raise ValueError("no personal details and no items: is it a CV?")
    return out


def check_contacts(extraction, found):
    """Keeps the e-mail, phone and links a model read only if the CV's text
    has them (documents.contacts found them), so that nothing made up gets
    in; fills in what the model missed."""
    personal = extraction["personal"]
    emails = [e.lower() for e in found["emails"]]
    if personal["email"].lower() not in emails:
        personal["email"] = found["emails"][0] if found["emails"] else ""
    phones = {re.sub(r"\D", "", p)[-9:] for p in found["phones"]}
    if re.sub(r"\D", "", personal["phone"])[-9:] not in phones:
        personal["phone"] = found["phones"][0] if found["phones"] else ""
    written = [_url_key(l) for l in found["links"]]
    links = [l for l in personal["links"]
             if any(_url_key(l) in w or w in _url_key(l) for w in written)]
    for link in found["links"]:
        if not any(_url_key(link) == _url_key(l) for l in links):
            links.append(link)
    personal["links"] = links[:MAX_LIST]
    return extraction


def _url_key(url):
    return re.sub(r"^(https?://)?(www\.)?", "", url.lower()).rstrip("/")


# --- telling whether two items are the same --------------------------------

LANGUAGE_NAMES = {
    "english": "angielski", "polish": "polski", "german": "niemiecki",
    "russian": "rosyjski", "french": "francuski", "spanish": "hiszpański",
    "italian": "włoski", "ukrainian": "ukraiński", "czech": "czeski",
    "portuguese": "portugalski", "dutch": "niderlandzki",
    "swedish": "szwedzki", "norwegian": "norweski", "japanese": "japoński",
    "chinese": "chiński", "korean": "koreański", "arabic": "arabski",
    "hungarian": "węgierski", "slovak": "słowacki", "latin": "łaciński",
    "deutsch": "niemiecki", "angielskiego": "angielski",
    "niemieckiego": "niemiecki", "rosyjskiego": "rosyjski",
}
# Words that tell nothing about which organization it is.
FILLER = {"sp", "z", "o", "oo", "sa", "s", "a", "ltd", "inc", "gmbh", "llc",
          "co", "the", "of", "and", "i", "w", "spolka", "akcyjna"}


def normalize(text):
    """Lowercase ASCII words: "Atende Industries Sp. z o.o." -> "atende
    industries"; "C++" and "C#" stay apart from "C"."""
    text = str(text or "").casefold().replace("ł", "l")
    text = text.replace("c++", "cpp").replace("c#", "csharp") \
        .replace("f#", "fsharp").replace(".net", "dotnet").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in FILLER) or " ".join(words)


def similarity(a, b):
    """0..1: how alike two names are; None when either is empty. All the
    words of the shorter one within the longer counts as alike (a name
    and its longer form)."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return None
    if a == b:
        return 1.0
    words_a, words_b = set(a.split()), set(b.split())
    if words_a <= words_b or words_b <= words_a:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _sim(a, b):
    s = similarity(a, b)
    return 0.0 if s is None else s


def _months(date, last=False):
    """A date as a count of months; a year alone is its January (or its
    December, `last`)."""
    if not date:
        return None
    year = int(date[:4])
    month = int(date[5:7]) if len(date) > 4 else (12 if last else 1)
    return year * 12 + month - 1


def _same_date(a, b):
    """True or False when both are known, at the precision of the coarser
    one; None otherwise."""
    if not a or not b:
        return None
    if len(a) == 4 or len(b) == 4:
        return a[:4] == b[:4]
    return a == b


def _span(item):
    start = _months(item.get("start"))
    if start is None:
        return None
    if item.get("current"):
        end = _months(datetime.date.today().strftime("%Y-%m"))
    else:
        end = _months(item.get("end"), last=True)
        if end is None:  # a single month, e.g. a summer internship
            end = _months(item.get("start"), last=True)
    return start, max(start, end)


def date_agreement(a, b):
    """How well two items' dates agree: 1 the same start and end, 0.7 the
    same start, 0.4 overlapping, 0 nothing to compare, -1 they disagree."""
    start = _same_date(a.get("start"), b.get("start"))
    end = _same_date(a.get("end"), b.get("end"))
    if a.get("current") and b.get("current"):
        end = True
    if start is True:
        return 1.0 if end is not False else 0.7
    span_a, span_b = _span(a), _span(b)
    if span_a and span_b:
        overlap = min(span_a[1], span_b[1]) - max(span_a[0], span_b[0])
        return 0.4 if overlap >= 0 and start is None else -1.0
    return 0.0 if start is None else -1.0


def _dated_match(a, b, place, what):
    """For jobs, schools, projects: the same place (`place` field) and
    agreeing dates, or the very same dates and something else alike."""
    dates = date_agreement(a, b)
    if dates < 0:
        return None
    places, whats = _sim(a.get(place), b.get(place)), _sim(a.get(what),
                                                           b.get(what))
    if places >= 0.75 and (whats >= 0.5 or dates >= 0.7) or \
            dates == 1.0 and (places >= 0.4 or whats >= 0.6) or \
            dates == 0.0 and places >= 0.8 and whats >= 0.8:
        return places + whats + dates
    return None


def _name_key(section, name):
    key = normalize(name)
    if section == "languages":
        key = LANGUAGE_NAMES.get(key.split(" ")[0], key.split(" ")[0]) \
            if key else key
        key = normalize(key)
    return key


def match(section, a, b):
    """A score (higher: more alike) if items a and b are the same thing,
    else None."""
    if section == "experience":
        return _dated_match(a, b, "organization", "title")
    if section == "education":
        score = _dated_match(a, b, "institution", "field")
        if score is None and date_agreement(a, b) >= 0.7 and \
                _sim(a.get("degree"), b.get("degree")) >= 0.6:
            score = 1.0 + date_agreement(a, b)
        return score
    if section == "projects":
        if date_agreement(a, b) < 0:
            return None
        s = _sim(a.get("name"), b.get("name"))
        return s if s >= 0.8 else None
    if section in ("skills", "languages", "interests"):
        if _name_key(section, a["name"]) == _name_key(section, b["name"]):
            return 2.0
        s = _sim(a["name"], b["name"])
        return s if s >= 0.92 and section != "skills" else None
    if section == "publications":
        if a.get("url") and a.get("url") == b.get("url"):
            return 2.0
        s = _sim(a.get("title"), b.get("title"))
        return s if s >= 0.85 else None
    # certificates, awards: alike names, and not different years
    if _same_date(a["date"][:4], b["date"][:4]) is False:
        return None
    s = _sim(a.get("name"), b.get("name"))
    return s if s >= 0.8 else None


def _stems(text):
    """The beginnings of a text's longer words: "systemu akwizycji" and
    "system akwizycja" have the same, whatever the inflection."""
    return {w[:5] for w in normalize(text).split() if len(w) >= 3}


def same_sentence(a, b):
    """Whether two entries of a list (e.g. achievements) say the same:
    alike as names, or mostly the same words."""
    if _sim(a, b) >= 0.85:
        return True
    stems_a, stems_b = _stems(a), _stems(b)
    least = min(len(stems_a), len(stems_b))
    return least >= 3 and len(stems_a & stems_b) / least >= 0.6


def _join_lists(lists):
    """Entries of several lists, ones saying the same (see same_sentence)
    only once, in the order of the lists."""
    out = []
    for entries in lists:
        for entry in entries or []:
            if not any(same_sentence(entry, kept) for kept in out):
                out.append(entry)
    return out[:MAX_LIST]


def _merge(section, items):
    """One item from several alike ones, newest first: each field from the
    first that has it, lists joined, the end of an employment with
    whether it's still going on taken from the same item."""
    merged = {}
    for key, _, kind, _ in FIELDS[section]:
        if kind == "list":
            merged[key] = _join_lists(i.get(key) for i in items)
        elif kind == "bool":
            continue
        else:
            merged[key] = next((i[key] for i in items if i.get(key)), "")
    if "current" in FIELDS_KEYS[section]:
        dated = next((i for i in items if i.get("end") or i.get("current")),
                     None)
        merged["end"] = dated["end"] if dated else ""
        merged["current"] = bool(dated and dated.get("current"))
    return merged


# --- building the profile ------------------------------------------------

def new_id(prefix):
    return prefix + secrets.token_hex(4)


def empty_overrides():
    return {"personal": {}, "items": {}, "removed": {}}


def build(cvs, overrides):
    """The profile from the CVs (dicts with id, uploaded_at and
    extraction, any order) and the user's overrides:

        personal  {field: {value, from: cv id or "user", cv: what the CVs
                  say when the user changed it}}
        sections  {section: [item + {id, sources: cv ids, refs, manual,
                  edited}]}
        removed   {section: [{id, label}]}
    """
    cvs = sorted((c for c in cvs if c.get("extraction")),
                 key=lambda c: (c["uploaded_at"], c["id"]), reverse=True)
    profile = {"personal": _build_personal(cvs, overrides["personal"]),
               "sections": {}, "removed": {}}
    for section in FIELDS:
        items, removed = _build_section(
            section, cvs, overrides["items"].get(section, []),
            overrides["removed"].get(section, []))
        profile["sections"][section] = _sorted(section, items)
        profile["removed"][section] = removed
    return profile


def _build_personal(cvs, chosen):
    personal = {}
    for key, _, kind, _ in PERSONAL:
        if kind == "list":
            value = _join_lists(c["extraction"]["personal"].get(key)
                                for c in cvs)
            source = cvs[0]["id"] if value else None
        else:
            source = next((c["id"] for c in cvs
                           if c["extraction"]["personal"].get(key)), None)
            value = next((c["extraction"]["personal"][key] for c in cvs
                          if c["id"] == source), "")
        entry = {"value": value, "from": source}
        if key in chosen:
            entry = {"value": chosen[key], "from": "user", "cv": value}
        personal[key] = entry
    return personal


def _build_section(section, cvs, manual, removed):
    clusters = [{"manual": m, "members": [], "cvs": set()} for m in manual]
    pinned = {ref: c for c in clusters for ref in c["manual"].get(
        "replaces", [])}
    tomb_refs = {ref for t in removed for ref in t["refs"]}
    for cv in cvs:
        for index, item in enumerate(cv["extraction"].get(section, [])):
            ref = "%s:%d" % (cv["id"], index)
            member = (ref, cv["id"], item)
            if ref in pinned:
                pinned[ref]["members"].append(member)
                pinned[ref]["cvs"].add(cv["id"])
                continue
            if ref in tomb_refs or any(
                    match(section, t["item"], item) is not None
                    for t in removed):
                continue
            best, best_score = None, None
            for cluster in clusters:
                if cv["id"] in cluster["cvs"]:
                    continue
                against = [cluster["manual"]] if cluster["manual"] else []
                against += [m[2] for m in cluster["members"]]
                scores = [s for s in (match(section, other, item)
                                      for other in against) if s is not None]
                if scores and (best_score is None or max(scores) > best_score):
                    best, best_score = cluster, max(scores)
            if best is None:
                best = {"manual": None, "members": [], "cvs": set()}
                clusters.append(best)
            best["members"].append(member)
            best["cvs"].add(cv["id"])
    items = []
    for cluster in clusters:
        refs = [m[0] for m in cluster["members"]]
        sources = sorted(cluster["cvs"])
        if cluster["manual"]:
            m = cluster["manual"]
            item = {k: m.get(k) for k in FIELDS_KEYS[section]}
            item.update(id=m["id"], manual=True,
                        edited=bool(m.get("replaces")))
        else:
            item = _merge(section, [m[2] for m in cluster["members"]])
            item.update(id="a" + min(refs, key=_ref_order).replace(":", "-"),
                        manual=False, edited=False)
        item.update(sources=sources, refs=refs)
        items.append(item)
    tombs = [{"id": t["id"], "label": label(section, t["item"])}
             for t in removed]
    return items, tombs


def _ref_order(ref):
    cv, index = ref.split(":")
    return int(cv), int(index)


def _sorted(section, items):
    if section in ("experience", "education", "projects"):
        def when(item):
            span = _span(item)
            return (bool(item.get("current")), span[1] if span else -1,
                    span[0] if span else -1)
        return sorted(items, key=when, reverse=True)
    if section in ("certificates", "awards", "publications"):
        return sorted(items, key=lambda i: i.get("date") or "", reverse=True)
    if section == "skills":
        order = []
        for item in items:
            if item["category"] not in order:
                order.append(item["category"])
        return sorted(items, key=lambda i: order.index(i["category"]))
    return items


def label(section, item):
    """A short name for an item, e.g. for the list of removed ones."""
    first = item.get(FIELDS[section][0][0]) or "?"
    second = {"experience": "organization", "education": "degree",
              "certificates": "issuer", "projects": "role",
              "publications": "venue", "awards": "issuer"}.get(section)
    when = item.get("start") or item.get("date") or ""
    parts = [first] + ([item[second]] if second and item.get(second) else [])
    return ", ".join(parts) + (" (%s)" % when[:4] if when else "")


def find(profile, section, item_id):
    return next((i for i in profile["sections"][section]
                 if i["id"] == item_id), None)


# --- the user's changes (on overrides, given the profile they see) ---------

def save_item(overrides, profile, section, item_id, fields):
    """The user's version of an item (item_id None: a new one); returns
    its id."""
    item = clean_item(section, fields)
    if not item:
        raise ValueError(FIELDS[section][0][1])
    mine = overrides["items"].setdefault(section, [])
    if item_id:
        current = find(profile, section, item_id)
        if not current:
            raise KeyError(item_id)
        refs = current["refs"]
        old = next((m for m in mine if m["id"] == item_id), None)
        if old:
            item.update(id=old["id"], replaces=sorted(
                set(old.get("replaces", [])) | set(refs), key=_ref_order))
            mine[mine.index(old)] = item
            return item["id"]
        item.update(id=new_id("m"), replaces=refs)
    else:
        if len(mine) >= MAX_ITEMS:
            raise ValueError("too many")
        item.update(id=new_id("m"), replaces=[])
    mine.append(item)
    return item["id"]


def remove_item(overrides, profile, section, item_id):
    """Takes an item out of the profile; what of the CVs it holds stays
    out (until restored)."""
    current = find(profile, section, item_id)
    if not current:
        raise KeyError(item_id)
    mine = overrides["items"].setdefault(section, [])
    overrides["items"][section] = [m for m in mine if m["id"] != item_id]
    if current["refs"] or not current["manual"]:
        snapshot = {k: current.get(k) for k in FIELDS_KEYS[section]}
        overrides["removed"].setdefault(section, []).append(
            {"id": new_id("r"), "refs": current["refs"], "item": snapshot})


def revert_item(overrides, section, item_id):
    """Drops the user's version of an edited item: the CVs' one is back."""
    mine = overrides["items"].get(section, [])
    if not any(m["id"] == item_id and m.get("replaces") for m in mine):
        raise KeyError(item_id)
    overrides["items"][section] = [m for m in mine if m["id"] != item_id]


def restore_item(overrides, section, tomb_id):
    tombs = overrides["removed"].get(section, [])
    if not any(t["id"] == tomb_id for t in tombs):
        raise KeyError(tomb_id)
    overrides["removed"][section] = [t for t in tombs if t["id"] != tomb_id]


def set_personal(overrides, values):
    """{field: value}; a value None goes back to what the CVs say."""
    kinds = {key: (kind, longest) for key, _, kind, longest in PERSONAL}
    for key, value in values.items():
        if key not in kinds:
            continue
        if value is None:
            overrides["personal"].pop(key, None)
        else:
            overrides["personal"][key] = clean_value(*(
                (kinds[key][0], value, kinds[key][1])))


def forget_cv(overrides, cv_id):
    """A CV is gone: refs to its items go, and so do removals that were
    only about it (an edited item stays, as the user wrote it)."""
    prefix = "%s:" % cv_id
    for items in overrides["items"].values():
        for item in items:
            item["replaces"] = [r for r in item.get("replaces", [])
                                if not r.startswith(prefix)]
    for section, tombs in overrides["removed"].items():
        for t in tombs:
            t["refs"] = [r for r in t["refs"] if not r.startswith(prefix)]


# --- what the program counts by itself --------------------------------------

DEGREES = [  # the highest first
    (4, "doktor", r"\b(dr|doktor\w*|ph\.? ?d|doctor\w*|doktoranck\w*)\b"),
    (3, "magister", r"\b(mgr|magist\w*|m\.? ?sc|master\w*|mba)\b"),
    (2, "inżynier / licencjat",
     r"\b(inż|inzynier\w*|inżynier\w*|licencja\w*|b\.? ?sc|bachelor\w*|"
     r"b\.? ?eng)\b"),
    (1, "średnie", r"\b(liceum|technikum|matura|high school)\b"),
]


def stats(profile):
    """What can be counted: months of work (overlapping jobs counted
    once), the current jobs, the highest finished degree, how many items
    each section has."""
    sections = profile["sections"]
    spans = sorted(s for s in (_span(i) for i in sections["experience"])
                   if s)
    months, last_end = 0, None
    for start, end in spans:
        if last_end is not None and start <= last_end:
            if end > last_end:
                months += end - last_end
                last_end = end
            continue
        months += end - start + 1
        last_end = end
    first = min((s[0] for s in spans), default=None)
    degree = None
    for item in sections["education"]:
        if item.get("current"):
            continue
        text = " ".join([item.get("degree") or "", item.get("field") or ""])
        for rank, name, pattern in DEGREES:
            if re.search(pattern, text, re.I) and (
                    degree is None or rank > degree[0]):
                degree = (rank, name)
    return {
        "experience_months": months,
        "career_start": "%04d" % (first // 12) if first is not None else "",
        "current_jobs": [label("experience", i) for i in
                         sections["experience"] if i.get("current")],
        "highest_degree": degree[1] if degree else "",
        "counts": {s: len(items) for s, items in sections.items()},
    }


def years_months(months):
    years, months = divmod(months, 12)
    parts = []
    if years:
        parts.append("%d %s" % (years, "rok" if years == 1 else "lata"
                                if years % 10 in (2, 3, 4) and
                                years % 100 not in (12, 13, 14) else "lat"))
    if months:
        parts.append("%d mies." % months)
    return " ".join(parts) or "0 mies."


# --- as text for a model ------------------------------------------------

def fmt_date(date):
    return "%s.%s" % (date[5:7], date[:4]) if len(date) > 4 else date


def fmt_span(item):
    start = fmt_date(item.get("start") or "")
    end = "obecnie" if item.get("current") else fmt_date(item.get("end") or "")
    if start and end and end != start:
        return "%s – %s" % (start, end)
    return start or end


def text(profile, about="", goals=""):
    """The profile as plain text, as compact as it can be."""
    p = {k: v["value"] for k, v in profile["personal"].items()}
    lines = []
    for key, label_, kind, _ in PERSONAL:
        if key in ("email", "phone", "links"):
            continue  # nothing a model needs to judge a candidate
        if p.get(key):
            lines.append("%s: %s" % (label_, p[key]))
    for section, title, spec in SECTIONS:
        items = profile["sections"][section]
        if not items:
            continue
        lines.append("")
        lines.append("## " + title)
        if section in ("skills", "languages", "interests"):
            groups = {}
            for i in items:
                name = i["name"] + (" (%s)" % i["level"] if i.get("level")
                                    else "")
                groups.setdefault(i.get("category") or "", []).append(name)
            for category, names in groups.items():
                lines.append(("%s: " % category if category else "") +
                             ", ".join(names))
            continue
        for i in items:
            when = fmt_span(i) if "start" in i else fmt_date(
                i.get("date") or "")
            head = [x for x in (i.get(f[0]) for f in spec[:3]
                                if f[2] == "text") if x]
            lines.append("- " + (when + ": " if when else "") +
                         ", ".join(head))
            for key, label_, kind, _ in spec[3:]:
                value = i.get(key)
                if not value or kind in ("date", "bool") or \
                        key in [f[0] for f in spec[:3]]:
                    continue
                if kind == "list":
                    lines.append("  %s: %s" % (label_, "; ".join(value)))
                else:
                    lines.append("  " + value.replace("\n", "\n  "))
    if about:
        lines += ["", "## Kandydat o sobie", about]
    if goals:
        lines += ["", "## Czego kandydat szuka", goals]
    return "\n".join(lines).strip()


def glossary(profile):
    """The names of what the profile holds, one line per section (with
    the achievements of jobs and projects), for a model reading another CV
    (agents.read_cv) to call the same things the same."""
    lines = []
    for section, title, spec in SECTIONS:
        names = []
        for item in profile["sections"][section]:
            more = "".join("\n  - " + h for h in item.get("highlights", []))
            if section == "experience":
                name = "%s – %s (%s)" % (item["title"], item["organization"],
                                         fmt_span(item))
            elif section == "education":
                name = "%s – %s, %s (%s)" % (
                    item["institution"], item["degree"], item["field"],
                    fmt_span(item))
            else:
                name = item[spec[0][0]]
                if item.get("category"):
                    name = "%s [%s]" % (name, item["category"])
            names.append(name + more)
        if names and section in ("experience", "education", "projects"):
            lines.append("%s:\n%s" % (title, "\n".join(
                "- " + n for n in names)))
        elif names:
            lines.append("%s: %s" % (title, "; ".join(names)))
    return "\n".join(lines)


def fingerprint(profile_text):
    return hashlib.sha256(profile_text.encode("utf-8")).hexdigest()[:16]


def public(profile):
    """The profile for the page: refs stay on the server."""
    out = json.loads(json.dumps(profile))
    for items in out["sections"].values():
        for item in items:
            item.pop("refs", None)
    return out
