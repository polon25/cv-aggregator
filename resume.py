"""A CV tailored to one job offer, made of the candidate's profile alone,
as a PDF (LaTeX, the moderncv class).

A model (agents.tailor_cv) gets the profile, its items named by refs
(sources(): "E1" the first job, "D1" the first school...), and the offer,
and answers with a plan (read_plan()): which items go in, their fields in
the CV's language, a few points on each job, the skills, grouped, and
a short summary. It writes nothing on its own that the program can fill
in: dates, employers' names, certificates' dates, publications, contact
details are taken from the profile by build(). Every job and school goes
in, so that the CV has no gaps.

What the model wrote is checked twice before it goes in:

1. by the program (build()): skills only by the names the profile has;
   technologies named for a job only if that job names them; no text
   may name what the offer asks for and the profile doesn't have, nor
   a number the profile doesn't hold;
2. by a model (agents.check_cv), which gets every sentence written
   (statements()) with what it was written from, names the ones the
   profile doesn't back and may write them anew so that it does (a
   faithful translation of a field, a point without what was made up);
   that goes in only if the program finds nothing wrong with it either
   (amend()).

What goes out is gone from the CV (a field goes back to what the profile
says); the user sees what was left out or changed, and why. latex() writes the
document, in Polish or English, as the user's own CVs are laid out, and
pdf() compiles it with pdflatex (config.PDFLATEX).
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata

import candidate
import config
import documents
import market
import matching

log = logging.getLogger("resume")

LANGUAGES = {"pl": "polski", "en": "angielski"}
# The letters of the refs of each section's items; skills go by name.
REFS = [("experience", "E"), ("education", "D"), ("projects", "P"),
        ("certificates", "C"), ("publications", "U"), ("awards", "A"),
        ("languages", "L"), ("interests", "I")]
# Fields the model writes anew, in the CV's language; the others are
# copied from the profile.
WRITTEN = {
    "experience": ("title", "organization", "location"),
    "education": ("institution", "degree", "field", "thesis"),
    "projects": ("name", "role", "description"),
    "certificates": ("name", "issuer"),
    "publications": (),
    "awards": ("name", "issuer", "description"),
    "languages": ("name", "level"),
    "interests": ("name",),
}
# Sections all of whose items go in, chosen by the model or not.
WHOLE = ("experience", "education", "languages")
MAX_POINTS = 5
MAX_POINT = 300
MAX_SUMMARY = 800
MAX_GROUPS = 8
# Seconds pdflatex may take.
COMPILE_SECONDS = 60


class CompileError(Exception):
    """pdflatex isn't there, or it couldn't make the PDF."""


# --- the profile, for the model ---------------------------------------------

def sources(profile, about=""):
    """(the profile as text, its items marked with refs; {ref: (section,
    item)})."""
    refs, lines = {}, []
    for section, letter in REFS:
        items = profile["sections"][section]
        if not items:
            continue
        lines += ["", "## " + candidate.TITLES[section]]
        for n, item in enumerate(items, 1):
            ref = "%s%d" % (letter, n)
            refs[ref] = (section, item)
            if section == "languages":
                lines.append("[%s] %s" % (ref, item["name"] + (
                    " (%s)" % item["level"] if item.get("level") else "")))
            elif section == "interests":
                lines.append("[%s] %s" % (ref, item["name"]))
            else:
                first, *rest = candidate.item_lines(section, item)
                lines.append("[%s] %s" % (ref, first))
                lines += rest
    skills = profile["sections"]["skills"]
    if skills:
        lines += ["", "## " + candidate.TITLES["skills"]]
        for item in skills:
            more = [x for x in (item.get("category"), item.get("level")) if x]
            lines.append("- %s%s" % (item["name"], " (%s)" % "; ".join(more)
                                     if more else ""))
    personal = profile["personal"]
    if personal["headline"]["value"]:
        lines.insert(0, "Nagłówek z CV: %s" % personal["headline"]["value"])
    if personal["summary"]["value"]:
        lines += ["", "## Podsumowanie z CV", personal["summary"]["value"]]
    if about:
        lines += ["", "## Kandydat o sobie", about]
    return "\n".join(lines).strip(), refs


def skill_names(profile):
    """{normalized name: the name as the profile has it} of every skill:
    the skills', then the ones named at jobs and projects."""
    names = {}
    for item in profile["sections"]["skills"]:
        names.setdefault(candidate.normalize(item["name"]), item["name"])
    for section in ("experience", "projects"):
        for item in profile["sections"][section]:
            for name in item.get("skills") or []:
                names.setdefault(candidate.normalize(name), name)
    names.pop("", None)
    return names


# --- what the model planned -------------------------------------------------

def read_plan(answer, refs):
    """The model's plan fitted to its shape, refs it made up left out:
    {headline, summary, sections: {section: [{ref, fields, points,
    skills}]}, skills: [{category, level, names}]}. Raises ValueError
    when there's nothing to make a CV of."""
    def text(value, longest):
        return candidate.clean_text(value, longest)

    plan = {"headline": text(answer.get("headline"), 200),
            "summary": candidate.clean_text(answer.get("summary"),
                                            MAX_SUMMARY, True),
            "sections": {}, "skills": []}
    for section, _ in REFS:
        entries, seen = [], set()
        for raw in answer.get(section) or []:
            if isinstance(raw, str):
                raw = {"ref": raw}
            if not isinstance(raw, dict):
                continue
            ref = str(raw.get("ref") or "").strip().upper()
            if ref not in refs or refs[ref][0] != section or ref in seen:
                continue
            seen.add(ref)
            fields = {key: text(raw.get(key), 400)
                      for key in WRITTEN[section]}
            entries.append({
                "ref": ref,
                "fields": {k: v for k, v in fields.items() if v},
                "points": candidate.clean_value("list", raw.get("points"),
                                                MAX_POINT)[:MAX_POINTS],
                "skills": candidate.clean_value("list", raw.get("skills"),
                                                60),
            })
        plan["sections"][section] = entries
    for raw in (answer.get("skills") or [])[:MAX_GROUPS]:
        if isinstance(raw, dict):
            names = candidate.clean_value("list", raw.get("names"), 60)
            if names:
                plan["skills"].append({
                    "category": text(raw.get("category"), 80),
                    "level": text(raw.get("level"), 60), "names": names})
    if not plan["sections"]["experience"] and not plan["skills"] and \
            not plan["summary"]:
        raise ValueError("an empty plan")
    return plan


# --- the CV, checked by the program -----------------------------------------

ABBREVIATION = re.compile(r"(?:\b\w{1,4}\.)+$")


def sentences(text):
    """A paragraph's sentences; a dot after an abbreviation ("Ph.D.",
    "inż.") doesn't end one."""
    out, start = [], 0
    for m in re.finditer(r"[.!?]\s+(?=[^\W\d_])", text):
        if not ABBREVIATION.search(text[start:m.start() + 1]) or \
                re.search(r"\w{5,}\.$", text[start:m.start() + 1]):
            out.append(text[start:m.start() + 1].strip())
            start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def _words(text):
    return " %s " % candidate.normalize(text)


def _numbers(text):
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", text)}


class Checker:
    """What a text written for the CV may say, by the program's lights:
    no skill the offer asks for that the profile doesn't name anywhere,
    no number the profile (or what the program counted of it) doesn't
    hold."""

    def __init__(self, profile, profile_text, offer, counted):
        self.words = _words(profile_text)
        self.numbers = _numbers(profile_text) | _numbers(counted)
        own = matching.skill_names(profile, {})
        self.missing = []
        for name in (offer.get("skills") or []) + (offer.get("nice") or []):
            key = candidate.normalize(name)
            if not key or matching.has_skill(name, own) or \
                    " %s " % key in self.words or \
                    all(w in market.GENERIC for w in key.split()):
                continue
            self.missing.append((key, name))

    def why_not(self, text):
        """Why a text can't go in the CV, or None."""
        words = _words(text)
        for key, name in self.missing:
            if " %s " % key in words:
                return "„%s” (z oferty) – tego nie ma w profilu" % name
        numbers = sorted(_numbers(text) - self.numbers)
        if numbers:
            return "liczba %s – nie ma jej w profilu" % numbers[0]
        return None


def build(profile, plan, refs, checker):
    """(the CV: {headline, summary: [sentences], sections: {section:
    [{ref, item, fields, points, skills}]}, skills: [{category, level,
    names}]}, what was left out: [{text, why}]), from the model's plan
    as the program checked it. Items keep the profile's order."""
    dropped = []

    def keep(text, why_else=None):
        why = why_else or checker.why_not(text)
        if why:
            dropped.append({"text": text, "why": why})
        return not why

    names = skill_names(profile)
    cv = {"headline": plan["headline"] if plan["headline"] and keep(
        plan["headline"]) else "",
        "summary": [s for s in sentences(plan["summary"]) if keep(s)],
        "sections": {}, "skills": []}
    for section, letter in REFS:
        planned = {e["ref"]: e for e in plan["sections"][section]}
        entries = []
        for ref, (where, item) in refs.items():
            if where != section or (ref not in planned and
                                    section not in WHOLE):
                continue
            entry = planned.get(ref) or {"fields": {}, "points": [],
                                         "skills": []}
            fields = {k: v for k, v in entry["fields"].items() if keep(v)}
            points = [p for p in entry["points"] if keep(p)]
            # technologies of a job: the ones it names, by their names
            own = {candidate.normalize(s): s for s in item.get("skills")
                   or []}
            said = _words(" ".join(candidate.item_lines(section, item)))
            skills = []
            for name in entry["skills"]:
                key = candidate.normalize(name)
                found = own.get(key) or (names.get(key) if " %s " % key
                                         in said else None)
                if found and found not in skills:
                    skills.append(found)
                elif not found:
                    keep(name, "nie ma tego w profilu przy %s" % (
                        candidate.label(section, item)))
            entries.append({"ref": ref, "item": item, "fields": fields,
                            "points": points, "skills": skills})
        cv["sections"][section] = entries
    levels = {candidate.normalize(i["name"]): i.get("level") or ""
              for i in profile["sections"]["skills"]}
    used = set()
    for group in plan["skills"]:
        chosen = []
        for name in group["names"]:
            key = candidate.normalize(name)
            if key not in names:
                keep(name, "tej umiejętności nie ma w profilu")
            elif key not in used:
                used.add(key)
                chosen.append(names[key])
        if not chosen:
            continue
        # a level only for skills the profile gives that same level
        known = {levels.get(candidate.normalize(n), "") for n in chosen}
        same = len({candidate.normalize(k) for k in known}) == 1 and \
            "" not in known
        level = group["level"] if same else ""
        if group["level"] and not level:
            dropped.append({"text": "%s: %s" % (group["level"],
                                                ", ".join(chosen)),
                            "why": "profil nie podaje tego poziomu dla "
                                   "wszystkich tych umiejętności"})
        cv["skills"].append({
            "category": group["category"] if group["category"] and keep(
                group["category"]) else "",
            "level": level if level and keep(level) else "",
            "names": chosen,
            "profile_level": levels[candidate.normalize(chosen[0])]
            if same else ""})
    return cv, dropped


# --- the CV, checked by a model ---------------------------------------------

def statements(cv, refs):
    """What of the CV a model wrote, for another to check:
    [{where, text, ref, original}]; `where` says where in the CV it is
    (for drop()), `original` is the field's value in the profile."""
    out = []
    if cv["headline"]:
        out.append({"where": ("headline",), "text": cv["headline"]})
    for n, sentence in enumerate(cv["summary"]):
        out.append({"where": ("summary", n), "text": sentence})
    for section, _ in REFS:
        for n, entry in enumerate(cv["sections"][section]):
            for key, value in entry["fields"].items():
                original = entry["item"].get(key) or ""
                if value != original:
                    out.append({"where": ("field", section, n, key),
                                "text": value, "ref": entry["ref"],
                                "original": original})
            for p, point in enumerate(entry["points"]):
                out.append({"where": ("point", section, n, p),
                            "text": point, "ref": entry["ref"]})
    for n, group in enumerate(cv["skills"]):
        if group["level"]:
            out.append({"where": ("level", n), "text": group["level"],
                        "original": group["profile_level"],
                        "about": ", ".join(group["names"])})
    return out


def amend(cv, found, verdicts, checker):
    """The CV with what the checking model found unsupported
    ({statement's index: {why, fix}}) written as it wrote it anew (if the
    program finds nothing wrong with that), or left out; returns
    [{text, why, fix}] of it."""
    dropped = []
    points, summary = {}, {}
    for index, verdict in sorted(verdicts.items()):
        statement = found[index]
        where = statement["where"]
        fix = verdict.get("fix") or ""
        if fix and (fix == statement["text"] or checker.why_not(fix)):
            fix = ""
        dropped.append({"text": statement["text"], "why": verdict["why"],
                        "fix": fix})
        if where[0] == "headline":
            cv["headline"] = fix
        elif where[0] == "summary":
            summary[where[1]] = fix
        elif where[0] == "field":
            fields = cv["sections"][where[1]][where[2]]["fields"]
            if fix:
                fields[where[3]] = fix
            else:
                fields.pop(where[3], None)
        elif where[0] == "point":
            points[where[1:]] = fix
        elif where[0] == "level":
            cv["skills"][where[1]]["level"] = fix
    cv["summary"] = [summary.get(n, s) for n, s in enumerate(cv["summary"])
                     if summary.get(n, s)]
    for section, entries in cv["sections"].items():
        for n, entry in enumerate(entries):
            entry["points"] = [points.get((section, n, i), p)
                               for i, p in enumerate(entry["points"])
                               if points.get((section, n, i), p)]
    return dropped


def public(cv):
    """The CV as the page shows it: what's in it, in short."""
    out = {"headline": cv["headline"], "summary": " ".join(cv["summary"]),
           "skills": [dict((k, g[k]) for k in ("category", "level", "names"))
                      for g in cv["skills"]], "sections": {}}
    for section, entries in cv["sections"].items():
        out["sections"][section] = [
            {"label": candidate.label(section, dict(e["item"],
                                                    **e["fields"])),
             "points": e["points"], "skills": e["skills"]}
            for e in entries]
    return out


# --- LaTeX ------------------------------------------------------------------

CONSENT = {
    "pl": "Wyrażam zgodę na przetwarzanie moich danych osobowych dla "
          "potrzeb niezbędnych do realizacji procesu rekrutacji (zgodnie "
          "z ustawą z dnia 10 maja 2018 roku o ochronie danych osobowych "
          "(Dz. U. z 2019 r. poz. 1781) oraz zgodnie z Rozporządzeniem "
          "Parlamentu Europejskiego i Rady (UE) 2016/679 z dnia 27 kwietnia "
          "2016 r. w sprawie ochrony osób fizycznych w związku "
          "z przetwarzaniem danych osobowych i w sprawie swobodnego "
          "przepływu takich danych oraz uchylenia dyrektywy 95/46/WE "
          "(RODO).",
    "en": "I consent to the processing of my personal data for the "
          "purposes of the recruitment process.",
}
WORDS = {
    "pl": {"babel": "polish", "summary": "Podsumowanie",
           "experience": "Doświadczenie zawodowe", "education": "Edukacja",
           "skills": "Umiejętności", "certificates": "Kursy i certyfikaty",
           "languages": "Znajomość języków", "projects": "Wybrane projekty",
           "publications": "Publikacje",
           "awards": "Nagrody i wyróżnienia", "interests": "Zainteresowania",
           "present": "obecnie", "technologies": "Wykorzystywane technologie",
           "thesis": "Temat pracy"},
    "en": {"babel": "british", "summary": "Profile",
           "experience": "Experience", "education": "Education",
           "skills": "Skills", "certificates": "Courses & certificates",
           "languages": "Languages", "projects": "Selected projects",
           "publications": "Publications", "awards": "Awards",
           "interests": "Interests", "present": "present",
           "technologies": "Technologies used", "thesis": "Thesis"},
}
# Characters LaTeX (T1, utf8 input) can't print, as ones it can.
PLAIN = {"–": "--", "—": "---", "„": ",,", "”": "''", "“": "``",
         "‘": "`", "’": "'", "…": "...", "•": "-", "→": "->", "←": "<-",
         "≥": ">=", "≤": "<=", "×": "x", "−": "-", " ": " ",
         " ": " ", " ": " ", "·": "-"}
SPECIAL = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$",
           "&": r"\&", "#": r"\#", "%": r"\%", "_": r"\_",
           "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
           '"': r"\textquotedbl{}", "<": r"\textless{}",
           ">": r"\textgreater{}", "|": r"\textbar{}"}


def _printable(char):
    """Whether pdflatex prints a character as it is: ASCII, Latin-1's and
    Latin Extended-A's letters (Polish, German, French...)."""
    code = ord(char)
    return 32 <= code < 127 or 0xC0 <= code <= 0x17F and code != 0xD7 \
        and code != 0xF7


def tex(text):
    """Text as LaTeX prints it, its special characters escaped, ones it
    can't print made plain or left out."""
    out = []
    for char in str(text or ""):
        if char in ("\n", "\t"):
            char = " "
        char = PLAIN.get(char, char)
        for c in char:
            if c in SPECIAL:
                out.append(SPECIAL[c])
            elif _printable(c):
                out.append(c)
            else:
                plain = unicodedata.normalize("NFKD", c)
                out.append("".join(x for x in plain if 32 <= ord(x) < 127
                                   and x not in SPECIAL))
    return "".join(out)


def _when(item, language):
    """Dates in LaTeX, as the user's CVs write them: "03.2024 --~06.2026",
    "04--07.2018", "07.2026 --~present", "08.2022"."""
    def fmt(date):
        return tex(candidate.fmt_date(date or ""))
    start, end = item.get("start") or "", item.get("end") or ""
    if item.get("current"):
        present = tex(WORDS[language]["present"])
        return "%s --~%s" % (fmt(start), present) if start else present
    if not start or not end or start == end:
        return fmt(start or end)
    if start[:4] == end[:4] and len(start) > 4 and len(end) > 4:
        return "%s--%s" % (start[5:7], fmt(end))
    return "%s --~%s" % (fmt(start), fmt(end))


def _years(item, language):
    """Years in LaTeX: "2019--2024"."""
    start, end = (item.get("start") or "")[:4], (item.get("end") or "")[:4]
    if item.get("current"):
        end = tex(WORDS[language]["present"])
    if start and end and start != end:
        return "%s--%s" % (start, end)
    return start or end


def _name(profile, language):
    """(first names, family name) as the CV's head shows them, with the
    candidate's doctorate (the program found it in the profile): "dr
    Jan" in Polish, "Kowalski, Ph.D." in English."""
    words = profile["personal"]["name"]["value"].split()
    if not words:
        return "", ""
    first, family = " ".join(words[:-1]), words[-1]
    if candidate.stats(profile)["highest_degree"] == "doktor":
        if language == "en":
            family += ", Ph.D."
        else:
            engineer = any(
                re.search(r"\binż|\binżynier", "%s %s" % (
                    i.get("degree") or "", i.get("field") or ""), re.I)
                for i in profile["sections"]["education"]
                if not i.get("current"))
            first = ("dr inż. " if engineer else "dr ") + first
    return first.strip(), family


def _link(url):
    """A link as the CV shows it ("linkedin.com/in/x"), or "" when it has
    characters better kept out of LaTeX."""
    url = re.sub(r"^(https?://)?(www\.)?", "", url.strip()).rstrip("/")
    return url if re.fullmatch(r"[A-Za-z0-9./:\-]+", url) else ""


def latex(profile, cv, language):
    """The CV as a LaTeX document (moderncv, the "classic" style, as the
    user's own CVs)."""
    w = WORDS[language]
    p = {k: v["value"] for k, v in profile["personal"].items()}
    first, family = _name(profile, language)
    head = [
        r"\documentclass[11pt,a4paper]{moderncv}",
        r"\moderncvstyle{classic}",
        r"\moderncvcolor{blue}",
        r"\usepackage[scale=0.8]{geometry}",
        r"\setlength{\hintscolumnwidth}{3.2cm}",
        r"\usepackage[%s]{babel}" % w["babel"],
        r"\usepackage[utf8]{inputenc}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{tgtermes}",
        r"\usepackage{needspace}",
        r"\hyphenpenalty=10000",
        r"\exhyphenpenalty=10000",
        r"\colorlet{lastnamecolor}{color0}",
        r"\colorlet{namecolor}{lastnamecolor}",
        r"\colorlet{firstnamecolor}{lastnamecolor}",
        r"\firstname{%s}" % tex(first),
        r"\familyname{%s}" % tex(family),
    ]
    if cv["headline"]:
        head.append(r"\title{%s}" % tex(cv["headline"]))
    phone = re.sub(r"[^0-9+() \-]", "", p.get("phone") or "").strip()
    if phone:
        head.append(r"\mobile{%s}" % tex(phone))
    if re.fullmatch(r"[\w.+\-]+@[\w\-]+(\.[\w\-]+)+", p.get("email") or ""):
        head.append(r"\email{%s}" % tex(p["email"]))
    links = [l for l in (_link(u) for u in p.get("links") or []) if l]
    if links:
        head.append(r"\homepage{%s}" % links[0])
    if len(links) > 1:
        head.append(r"\extrainfo{%s}" % r" \textbullet{} ".join(links[1:4]))
    name = tex(" ".join(profile["personal"]["name"]["value"].split()))
    body = [r"\begin{document}",
            r"\hypersetup{pdfauthor={%s}, pdftitle={%s - CV}}" % (name, name),
            r"\makecvtitle"]

    def section(title):
        body.append("")
        body.append(r"\needspace{6\baselineskip}")
        body.append(r"\section{%s}" % tex(title))

    def field(entry, key):
        return entry["fields"].get(key) or entry["item"].get(key) or ""

    def items(points):
        if not points:
            return ""
        return "\n".join([r"\begin{itemize}"] + [
            r"\item %s" % tex(p) for p in points] + [r"\end{itemize}"])

    if cv["summary"]:
        section(w["summary"])
        body.append(r"\cvitem{}{%s}" % tex(" ".join(cv["summary"])))
    entries = cv["sections"]
    if entries["experience"]:
        section(w["experience"])
        for e in entries["experience"]:
            points = list(e["points"])
            if e["skills"]:
                points.append("%s: %s" % (w["technologies"],
                                          ", ".join(e["skills"])))
            title, organization = field(e, "title"), field(e, "organization")
            # in Polish the job comes first, in English the employer
            first, second = (organization, title) if language == "en" \
                else (title, organization)
            body.append(r"\cventry{%s}{%s}{%s}{%s}{}{%s}" % (
                _when(e["item"], language), tex(first), tex(second),
                tex(field(e, "location")), items(points)))
    if entries["education"]:
        section(w["education"])
        for e in entries["education"]:
            years, degree = _years(e["item"], language), field(e, "degree")
            thesis = field(e, "thesis")
            body.append(r"\cventry{%s}{%s}{%s}{}{}{%s}" % (
                years + (r"\\" + tex(degree) if degree else ""),
                tex(field(e, "institution")), tex(field(e, "field")),
                r"%s: \textit{%s}" % (tex(w["thesis"]), tex(thesis))
                if thesis else ""))
    if cv["skills"] or entries["certificates"]:
        section(w["skills"])
        last = None
        for group in cv["skills"]:
            if group["category"] and group["category"] != last:
                body.append(r"\subsection{%s}" % tex(group["category"]))
            last = group["category"]
            body.append(r"\cvitem{%s}{%s}" % (tex(group["level"]),
                                              tex(", ".join(group["names"]))))
        if entries["certificates"]:
            body.append(r"\subsection{%s}" % tex(w["certificates"]))
            for e in entries["certificates"]:
                issuer = field(e, "issuer")
                body.append(r"\cvitem{%s}{%s}" % (
                    tex((e["item"].get("date") or "")[:4]),
                    tex(field(e, "name") + (" (%s)" % issuer if issuer
                                            else ""))))
    if entries["languages"]:
        section(w["languages"])
        body.append(r"\cvitem{}{%s}" % tex(", ".join(
            field(e, "name") + (" (%s)" % field(e, "level")
                                if field(e, "level") else "")
            for e in entries["languages"])))
    if entries["projects"]:
        section(w["projects"])
        for e in entries["projects"]:
            points = [field(e, "description")] if field(e, "description") \
                else []
            points += e["points"]
            if e["skills"]:
                points.append("%s: %s" % (w["technologies"],
                                          ", ".join(e["skills"])))
            body.append(r"\cventry{%s}{%s}{%s}{}{}{%s}" % (
                _when(e["item"], language), tex(field(e, "name")),
                tex(field(e, "role")), items(points)))
    if entries["publications"]:
        section(w["publications"])
        for e in entries["publications"]:
            i = e["item"]
            text = "%s: %s" % (tex(i.get("authors")), tex(i["title"])) \
                if i.get("authors") else tex(i["title"])
            if i.get("venue"):
                text += r", \emph{%s}" % tex(i["venue"])
            if i.get("url"):
                text += ", %s" % tex(i["url"])
            body.append(r"\cvitem{%s}{%s}" % (tex((i.get("date") or "")[:4]),
                                              text))
    if entries["awards"]:
        section(w["awards"])
        for e in entries["awards"]:
            text = ", ".join(x for x in (field(e, "name"), field(e, "issuer"))
                             if x)
            if field(e, "description"):
                text += " -- " + field(e, "description")
            body.append(r"\cvitem{%s}{%s}" % (
                tex((e["item"].get("date") or "")[:4]), tex(text)))
    if entries["interests"]:
        section(w["interests"])
        body.append(r"\renewcommand{\listitemsymbol}{-~}")
        names = [field(e, "name") for e in entries["interests"]]
        for n in range(0, len(names) - 1, 2):
            body.append(r"\cvlistdoubleitem{%s}{%s}" % (tex(names[n]),
                                                        tex(names[n + 1])))
        if len(names) % 2:
            body.append(r"\cvlistitem{%s}" % tex(names[-1]))
    body += ["", r"\vfill", r"\begin{center}",
             r"\textit{\small %s}" % tex(CONSENT[language]),
             r"\end{center}", "", r"\end{document}", ""]
    return "\n".join(head + [""] + body)


# --- compiling --------------------------------------------------------------

def can_compile():
    return bool(shutil.which(config.PDFLATEX))


def pdf(document):
    """The PDF (bytes) of a LaTeX document, compiled in a folder of its
    own; raises CompileError."""
    with tempfile.TemporaryDirectory(prefix="cv-") as folder:
        with open(os.path.join(folder, "cv.tex"), "w",
                  encoding="utf-8") as f:
            f.write(document)
        for _ in range(2):  # again if references changed
            try:
                subprocess.run(
                    [config.PDFLATEX, "-interaction=nonstopmode",
                     "-halt-on-error", "-no-shell-escape", "cv.tex"],
                    cwd=folder, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=COMPILE_SECONDS)
            except FileNotFoundError:
                raise CompileError("no %s" % config.PDFLATEX)
            except subprocess.TimeoutExpired:
                raise CompileError("%s took over %d s" % (
                    config.PDFLATEX, COMPILE_SECONDS))
            try:
                with open(os.path.join(folder, "cv.log"), "rb") as f:
                    log_text = f.read().decode("utf-8", "replace")
            except OSError:
                log_text = ""
            path = os.path.join(folder, "cv.pdf")
            if not os.path.exists(path) or "\n! " in log_text:
                errors = [l for l in log_text.splitlines()
                          if l.startswith("!")]
                raise CompileError("; ".join(errors[:3]) or "no PDF")
            if "Rerun to get" not in log_text:
                break
        with open(path, "rb") as f:
            return f.read()


def filename(profile, offer, language):
    """"CV_Jan_Kowalski_Firma.pdf", in ASCII."""
    def ascii_words(text):
        text = unicodedata.normalize("NFKD", str(text or "").replace(
            "ł", "l").replace("Ł", "L"))
        return "_".join(re.sub(r"[^A-Za-z0-9]+", " ", text).split()[:4])
    parts = ["CV", ascii_words(profile["personal"]["name"]["value"]),
             ascii_words(offer.get("company")), language.upper()]
    return "_".join(p for p in parts if p)


def language_of(offer, text):
    """The language a CV for the offer is best in: the offer's, when it's
    Polish or English; English for other languages, Polish when it can't
    be told."""
    found = documents.language("%s\n%s" % (offer.get("title") or "", text))
    if found in LANGUAGES:
        return found
    return "en" if found else "pl"
