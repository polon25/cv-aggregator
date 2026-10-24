"""What the models do, and only that: read_cv() turns the text of one CV
into a structured extraction (candidate.py merges those and does the
rest), analyze() describes a candidate from their whole profile,
review_offer() rates a job offer for a candidate (matching.py chooses
which).

Both ask for a JSON answer (llm.ask_json) and check it before it's kept.
Prompts are in Polish: so is what the models write.
"""

import json
import logging

import candidate
import documents
import llm
import matching

log = logging.getLogger("agents")


def _example(fields):
    return {key: {"text": "", "textarea": "", "date": "RRRR-MM",
                  "bool": False, "list": []}[kind]
            for key, _, kind, _ in fields}


EXTRACTION_SHAPE = json.dumps(dict(
    personal=_example(candidate.PERSONAL),
    **{name: [_example(fields)] for name, _, fields in candidate.SECTIONS}),
    ensure_ascii=False)

READ_CV = """Jesteś starannym asystentem rekrutera. Dostajesz tekst jednego CV \
(wyciągnięty z pliku, więc układ kolumn bywa pomieszany) i przepisujesz \
wszystko, co w nim jest, do JSON-a o takiej postaci:

%s

Zasady:
- Odpowiedz wyłącznie tym obiektem JSON, bez komentarzy, zwięźle: bez \
wcięć i bez pustych pól (pomiń klucze, dla których CV nic nie mówi).
- Pisz po polsku, także gdy CV jest w innym języku: tłumacz stanowiska, \
opisy, kierunki, stopnie, umiejętności opisowe i poziomy. Nazwy instytucji, \
które mają powszechnie używaną polską nazwę, podawaj po polsku (np. \
"Warsaw University of Technology" → "Politechnika Warszawska"); nazwy firm, \
technologii, certyfikatów i tytuły publikacji zostaw w oryginale.
- Niczego nie wymyślaj i nie dopowiadaj. Czego w CV nie ma, zostaw puste \
("", [], false). Nie pomijaj niczego, co w CV jest.
- Daty jako "RRRR-MM" albo "RRRR", gdy podany jest tylko rok. Jednorazowy \
wpis z jednym miesiącem (np. "08.2022") ma "start" i "end" takie same. \
"obecnie"/"present" → "current": true i puste "end".
- Każde stanowisko to osobny wpis w "experience", także kilka stanowisk \
u tego samego pracodawcy. Jeśli kilka wpisów dzieli jeden opis, przypisz go \
do wpisu, pod którym stoi w CV.
- "highlights": zadania i osiągnięcia z punktów pod stanowiskiem, każde \
osobno; "skills" przy stanowisku/projekcie: wymienione tam technologie.
- "skills": każda umiejętność osobno (np. "C++", "Python", "LabVIEW"), \
z kategorią z CV (np. "Języki programowania", "Narzędzia") i poziomem, jeśli \
CV go podaje (np. "dobry", "podstawowy").
- "languages": języki z poziomem jak w CV (np. "C1", "zaawansowany", \
"ojczysty"); język ojczysty tylko, jeśli CV go wymienia.
- "education.degree": stopień lub tytuł (np. "doktor", "magister inżynier", \
"inżynier", "licencjat", "studia doktoranckie"); "field": kierunek \
i specjalność.
- "personal.name": samo imię i nazwisko, bez tytułów naukowych. \
"headline": tytuł zawodowy lub nagłówek z CV, jeśli jest (nie tytuł \
naukowy). "summary": \
akapit podsumowania/profilu z CV, jeśli jest.
- Klauzule o przetwarzaniu danych osobowych pomiń.""" % EXTRACTION_SHAPE

KNOWN = """Profil kandydata ma już te wpisy z jego innych CV. Gdy to CV opisuje \
to samo, użyj dokładnie tych samych sformułowań (stanowisk, instytucji, \
stopni, kierunków, umiejętności, języków, zainteresowań, a także zadań \
i osiągnięć: to samo zadanie przepisz tak, jak jest w profilu), nawet jeśli \
to CV jest w innym języku; to, czego tu nie ma, opisz normalnie.

<profil>
%s
</profil>"""


def read_cv(text, filename, account, known=""):
    """What a model reads from a CV's text: (extraction, who answered).
    `known` (candidate.glossary()) are the names the profile has from the
    other CVs, which the model is asked to use for the same things, so
    that the CVs can be merged. The contact details are checked against
    the text."""
    found = documents.contacts(text)

    def validate(answer):
        return candidate.check_contacts(
            candidate.clean_extraction(answer), found)

    messages = [
        {"role": "system", "content": READ_CV},
        {"role": "user", "content": (KNOWN % known + "\n\n" if known else "")
         + "Plik: %s\n\n<cv>\n%s\n</cv>" % (filename, text)},
    ]
    # copying out needs no reasoning first
    extraction, _, model = llm.ask_json(messages, "read cv", validate,
                                        account, think=False)
    log.info("Read %s: %s", filename, ", ".join(
        "%s %d" % (s, len(extraction[s])) for s in candidate.FIELDS
        if extraction[s]))
    return extraction, model


ANALYZE = """Jesteś doświadczonym doradcą kariery i rekruterem. Dostajesz \
profil kandydata zebrany z jego CV (i to, co sam o sobie napisał) oraz \
policzone przez program fakty. Napisz po polsku rzetelną, konkretną analizę, \
opartą wyłącznie na tym profilu – bez ogólników, które pasowałyby do \
każdego.

Odpowiedz wyłącznie obiektem JSON:
{
 "summary": "3–5 zdań: kim jest kandydat zawodowo, co go wyróżnia, na \
jakim jest etapie kariery",
 "strengths": [{"title": "krótko", "detail": "1–2 zdania, z odwołaniem do \
konkretów z profilu"}],
 "weaknesses": [{"title": "krótko", "detail": "1–2 zdania: luka lub \
ryzyko w oczach pracodawcy i jak można je zmniejszyć"}],
 "career_paths": [{"title": "nazwa ścieżki", "fit": "wysokie|średnie|niskie", \
"why": "dlaczego pasuje", "gaps": "czego brakuje, by w nią wejść", \
"roles": ["stanowiska, na które warto aplikować"], "keywords": ["słowa \
kluczowe do wyszukiwania ofert, po polsku i angielsku"]}]
}

3–6 mocnych stron, 2–5 słabych, 3–5 ścieżek kariery od najlepiej \
pasującej; co najmniej jedna może być mniej oczywista, a realna. Staż \
pracy i najwyższy stopień bierz z faktów policzonych przez program."""


def _validate_analysis(answer):
    def pairs(key, least):
        out = []
        for entry in answer.get(key) or []:
            if isinstance(entry, dict) and str(entry.get("title") or "").strip():
                out.append({"title": candidate.clean_text(entry["title"], 200),
                            "detail": candidate.clean_text(
                                entry.get("detail"), 1000)})
        if len(out) < least:
            raise ValueError("fewer than %d %s" % (least, key))
        return out[:8]

    summary = candidate.clean_text(answer.get("summary"), 3000, True)
    if not summary:
        raise ValueError("no summary")
    paths = []
    for p in answer.get("career_paths") or []:
        if not isinstance(p, dict) or not str(p.get("title") or "").strip():
            continue
        fit = str(p.get("fit") or "").strip().lower()
        paths.append({
            "title": candidate.clean_text(p["title"], 200),
            "fit": fit if fit in ("wysokie", "średnie", "niskie") else "",
            "why": candidate.clean_text(p.get("why"), 1500),
            "gaps": candidate.clean_text(p.get("gaps"), 1500),
            "roles": candidate.clean_value("list", p.get("roles"), 100),
            "keywords": candidate.clean_value("list", p.get("keywords"), 60),
        })
    if not paths:
        raise ValueError("no career paths")
    return {"summary": summary, "strengths": pairs("strengths", 1),
            "weaknesses": pairs("weaknesses", 1), "career_paths": paths[:6]}


def facts(stats):
    """What the program counted, as text for a model."""
    lines = ["Łączny staż pracy (nakładające się okresy liczone raz): %s"
             % candidate.years_months(stats["experience_months"])]
    if stats["career_start"]:
        lines.append("Pierwsza praca: %s" % stats["career_start"])
    if stats["current_jobs"]:
        lines.append("Obecnie: %s" % "; ".join(stats["current_jobs"]))
    if stats["highest_degree"]:
        lines.append("Najwyższy ukończony poziom wykształcenia: %s"
                     % stats["highest_degree"])
    return "\n".join(lines)


def analyze(profile_text, stats, account):
    """(analysis, who answered) for a profile written by candidate.text()."""
    messages = [
        {"role": "system", "content": ANALYZE},
        {"role": "user", "content": "Fakty policzone przez program:\n%s\n\n"
         "<profil>\n%s\n</profil>" % (facts(stats), profile_text)},
    ]
    analysis, _, model = llm.ask_json(messages, "analyze", _validate_analysis,
                                      account)
    return analysis, model


REVIEW = """Jesteś doświadczonym rekruterem i doradcą kariery. Oceniasz, jak \
dobrze oferta pracy pasuje do kandydata: do jego doświadczenia i umiejętności \
oraz do tego, czym chce się zajmować. Bądź rzetelny i konkretny, odwołuj się \
do faktów z profilu i z oferty; nie zawyżaj ocen. Miejsca i trybu pracy, \
wynagrodzenia, rodzaju umowy ani poziomu stanowiska względem oczekiwań \
kandydata nie oceniaj i nie wymieniaj w zaletach ani wadach: to sprawdza \
osobno program. Oceniaj, czy kandydat poradzi sobie z tą pracą i czy to \
praca, którą chce wykonywać.

Odpowiedz wyłącznie obiektem JSON, w tej kolejności pól:
{"summary": "1–2 zdania: czym jest ta praca i jak ma się do kandydata", \
"pros": ["co pasuje"], "cons": ["czego brakuje albo co nie pasuje do \
oczekiwań"], "score": 0–100}

Zalet i wad podaj po 2–4, każdą krótko (do jednego zdania), najważniejsze \
najpierw. Nie powtarzaj tego, co oczywiste z samej oferty.

Skala: 85–100 świetne dopasowanie, kandydat spełnia prawie wszystkie wymagania \
i oferta odpowiada temu, czego szuka; 65–84 dobre, z drobnymi brakami; 40–64 \
częściowe, wyraźne braki albo rozjazd z oczekiwaniami; poniżej 40 słabe. \
Poziom stanowiska (junior/senior) zestaw ze stażem kandydata.

<kandydat>
%s
</kandydat>

<czego_szuka>
%s
</czego_szuka>"""


def _offer_text(offer, text):
    names = {"remote": "zdalnie", "hybrid": "hybrydowo",
             "office": "stacjonarnie"}
    contracts = dict(matching.CONTRACTS)
    levels = dict(matching.LEVELS)
    lines = ["Stanowisko: %s" % offer["title"],
             "Firma: %s" % offer["company"]]
    if offer["cities"] or offer["mode"]:
        lines.append("Miejsce: %s" % ", ".join(
            filter(None, [", ".join(offer["cities"][:5]),
                          names.get(offer["mode"], "")])))
    if offer.get("salary_min"):
        lines.append("Wynagrodzenie: %s–%s %s miesięcznie" % (
            offer["salary_min"], offer["salary_max"], offer["currency"]))
    if offer["contracts"]:
        lines.append("Umowa: %s" % ", ".join(
            contracts.get(c, c) for c in offer["contracts"]))
    if offer["level"]:
        lines.append("Poziom: %s" % levels.get(offer["level"],
                                               offer["level"]))
    if offer["skills"]:
        lines.append("Wymagane umiejętności: %s" % ", ".join(offer["skills"]))
    if offer["nice"]:
        lines.append("Mile widziane: %s" % ", ".join(offer["nice"]))
    if text:
        lines += ["", text]
    return "\n".join(lines)


def _validate_review(answer):
    try:
        score = int(round(float(answer.get("score"))))
    except (TypeError, ValueError):
        raise ValueError("no score")
    summary = candidate.clean_text(answer.get("summary"), 600)
    if not summary:
        raise ValueError("no summary")
    return {"summary": summary, "score": max(0, min(100, score)),
            "pros": candidate.clean_value("list", answer.get("pros"), 300)[:5],
            "cons": candidate.clean_value("list", answer.get("cons"), 300)[:5]}


def review_offer(profile_text, wishes_text, offer, text, account):
    """(review: {summary, pros, cons, score}, who answered) of an offer
    for a candidate: how it fits their experience, skills and what they
    want to do (matching.judge weighs the rest). The candidate goes in
    the system prompt, the same for all their offers (cached by models
    that cache)."""
    messages = [
        {"role": "system", "content": REVIEW % (
            profile_text, wishes_text or "(nie napisał)")},
        {"role": "user", "content": "<oferta>\n%s\n</oferta>"
         % _offer_text(offer, text)},
    ]
    review, _, model = llm.ask_json(messages, "review offer",
                                    _validate_review, account, think=False)
    return review, model
