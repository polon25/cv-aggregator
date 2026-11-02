"""The background work: reading uploaded CVs and writing analyses of
profiles, by config.WORKERS threads. Work waits in the database (a CV or
an analysis with status "pending"), so a restart loses none; wake() is
called when there's new work.

A CV is read in two steps: its text, by the program (documents.py), then
its contents, by a model (agents.read_cv) told what the profile has from
the user's other CVs. Once a user's last waiting CV is read, their profile
gets a new analysis. Advice on what to learn (market.py, agents.advise) is
written here too, when asked for (offers.py asks once a day, after rating
offers), and CVs for offers, when a user asks for one (resume.py).
"""

import logging
import threading

import agents
import candidate
import config
import db
import documents
import llm
import market
import offers
import resume

log = logging.getLogger("worker")

IDLE_SECONDS = 30
_wake = threading.Event()
_claim = threading.Lock()

NO_KEY = "Brak klucza API OpenRoutera: podaj swój w Ustawieniach."
BAD_KEY = "OpenRouter nie przyjął klucza API – sprawdź go w Ustawieniach."
FAILED = ("Żaden model nie dał użytecznej odpowiedzi (przeciążone albo "
          "brak środków). Spróbuj ponownie za chwilę.")
PAID_FAILED = ("Płatne modele nie dały użytecznej odpowiedzi (przeciążone "
               "albo brak środków na kluczu), a darmowe są wyłączone "
               "w Ustawieniach. Spróbuj ponownie za chwilę.")
NO_MODELS = ("Wszystkie modele są wyłączone w Ustawieniach: włącz płatne "
             "albo darmowe.")
NO_FUNDS = ("Na kluczu API skończyły się środki na płatne modele, a darmowe "
            "są wyłączone w Ustawieniach.")
NO_LATEX = ("Na serwerze nie ma LaTeXa (pdflatex), więc CV nie da się "
            "złożyć do PDF-a. Administrator: sudo apt install "
            "texlive-latex-extra texlive-lang-polish "
            "texlive-fonts-recommended.")


def wake():
    _wake.set()


def start(count):
    for n in range(count):
        threading.Thread(target=_run, name="worker-%d" % (n + 1),
                         daemon=True).start()


def _run():
    while True:
        try:
            if _work_once():
                continue
        except Exception:
            log.exception("Work failed")
        _wake.wait(IDLE_SECONDS)
        _wake.clear()


def _work_once():
    """Does one piece of work; False when there was none."""
    conn = db.connect()
    try:
        with _claim:
            cv = db.claim_cv(conn)
            wanted = None if cv else db.claim_resume(conn)
            user_id = None if cv or wanted else db.claim_analysis(conn)
            growth = None if cv or wanted or user_id else \
                db.claim_growth(conn)
        if cv:
            read_cv(conn, cv)
            return True
        if wanted:
            tailor(conn, wanted)
            return True
        if user_id:
            analyze(conn, user_id)
            return True
        if growth:
            advise(conn, growth)
            return True
        return False
    finally:
        conn.close()


def _error(e, account):
    if isinstance(e, llm.BadKey):
        return BAD_KEY if "no API key" not in str(e) else NO_KEY
    if isinstance(e, llm.NoModels):
        return NO_FUNDS if account["use_paid"] else NO_MODELS
    return FAILED if account["use_free"] else PAID_FAILED


def read_cv(conn, cv):
    log.info("Reading CV %d (%s) of user %d", cv["id"], cv["filename"],
             cv["user_id"])
    try:
        text = documents.extract(db.cv_path(cv), cv["kind"])
    except documents.Unreadable as e:
        db.finish_cv(conn, cv["id"], error=str(e))
        log.info("CV %d unreadable: %s", cv["id"], e)
        return _after_cv(conn, cv["user_id"])
    except OSError as e:
        log.warning("CV %d: %s", cv["id"], e)
        db.finish_cv(conn, cv["id"], error="Nie ma pliku tego CV na serwerze.")
        return _after_cv(conn, cv["user_id"])
    text = text[:config.MAX_CV_CHARS]
    db.save_cv_text(conn, cv["id"], text, documents.language(text))
    # the profile from the other CVs, for the model to call things the same
    others = [c for c in db.user_cvs(conn, cv["user_id"])
              if c["id"] != cv["id"]]
    row = db.get_profile(conn, cv["user_id"])
    known = candidate.glossary(candidate.build(others, row["overrides"]))
    account = db.account(conn, cv["user_id"])
    try:
        extraction, model = agents.read_cv(text, cv["filename"], account,
                                           known)
    except llm.LLMError as e:
        log.warning("CV %d not read: %s", cv["id"], e)
        db.finish_cv(conn, cv["id"], error=_error(e, account))
        return _after_cv(conn, cv["user_id"])
    db.finish_cv(conn, cv["id"], extraction=extraction, model=model)
    _after_cv(conn, cv["user_id"])


def _after_cv(conn, user_id):
    """Once none of the user's CVs waits any more, the profile gets a new
    analysis (if anything was read)."""
    if not db.cvs_busy(conn, user_id) and any(
            c["extraction"] for c in db.user_cvs(conn, user_id)):
        db.request_analysis(conn, user_id)
        wake()


def advise(conn, user_id):
    """Advice on what to learn, from the offers kept for the user
    (market.py)."""
    profile, row, _ = db.built_profile(conn, user_id)
    demand = market.demand(conn, user_id, profile, row["wishes"])
    if not demand["offers"]:
        db.finish_growth(conn, user_id, error="Nie ma jeszcze ofert, z których "
                         "można by wyciągnąć wnioski.")
        return
    account = db.account(conn, user_id)
    log.info("Advising user %d from %d offers", user_id, demand["offers"])
    try:
        advice, model = agents.advise(db.profile_text(profile, row), demand,
                                      market.cons(conn, user_id), account)
    except llm.LLMError as e:
        log.warning("Advice for user %d failed: %s", user_id, e)
        db.finish_growth(conn, user_id, error=_error(e, account))
        return
    db.finish_growth(conn, user_id, advice, model)


def analyze(conn, user_id):
    profile, row, _ = db.built_profile(conn, user_id)
    text = db.profile_text(profile, row)
    if not text:
        db.finish_analysis(conn, user_id, error="Profil jest pusty: wgraj CV "
                           "albo uzupełnij go ręcznie.")
        return
    log.info("Analyzing the profile of user %d", user_id)
    account = db.account(conn, user_id)
    try:
        analysis, model = agents.analyze(text, candidate.stats(profile),
                                         account)
    except llm.LLMError as e:
        log.warning("Analysis for user %d failed: %s", user_id, e)
        db.finish_analysis(conn, user_id, error=_error(e, account))
        return
    db.finish_analysis(conn, user_id, analysis, candidate.fingerprint(text),
                       model)


def tailor(conn, wanted):
    """A CV for an offer ({user_id, offer_id, language}): planned by a
    model, checked by the program and by another model, compiled
    (resume.py)."""
    try:
        _tailor(conn, wanted["user_id"], wanted["offer_id"],
                wanted["language"])
    except Exception:
        db.finish_resume(conn, wanted["user_id"], wanted["offer_id"],
                         error="Błąd serwera przy tworzeniu CV.")
        raise


def _tailor(conn, user_id, offer_id, language):
    offer = db.get_match(conn, user_id, offer_id)
    if not offer:
        db.finish_resume(conn, user_id, offer_id, error="Tej oferty już nie "
                         "ma.")
        return
    if not resume.can_compile():
        db.finish_resume(conn, user_id, offer_id, error=NO_LATEX)
        return
    profile, row, _ = db.built_profile(conn, user_id)
    sources = resume.sources(profile, row["about"])
    if not sources[0]:
        db.finish_resume(conn, user_id, offer_id, error="Profil jest pusty: "
                         "wgraj CV albo uzupełnij go ręcznie.")
        return
    text = offers.offer_text(conn, offer) or ""
    language = language or resume.language_of(offer, text)
    counted = agents.facts(candidate.stats(profile))
    account = db.account(conn, user_id)
    log.info("Making a CV (%s) for offer %d of user %d", language, offer_id,
             user_id)
    try:
        plan, model = agents.tailor_cv(sources, counted, offer, text,
                                       language, account)
        checker = resume.Checker(profile, sources[0], offer, counted)
        cv, dropped = resume.build(profile, plan, sources[1], checker)
        found = resume.statements(cv, sources[1])
        if found:
            verdicts, _ = agents.check_cv(sources[0], counted, found,
                                          account)
            dropped += resume.amend(cv, found, verdicts, checker)
    except llm.LLMError as e:
        log.warning("CV for offer %d of user %d failed: %s", offer_id,
                    user_id, e)
        db.finish_resume(conn, user_id, offer_id, error=_error(e, account))
        return
    document = resume.latex(profile, cv, language)
    try:
        pdf = resume.pdf(document)
    except resume.CompileError as e:
        log.warning("CV for offer %d of user %d not compiled: %s", offer_id,
                    user_id, e)
        db.finish_resume(conn, user_id, offer_id, error="Nie udało się "
                         "złożyć PDF-a (błąd LaTeXa: %s)." % str(e)[:300])
        return
    log.info("CV for offer %d of user %d: %d bytes, %d left out", offer_id,
             user_id, len(pdf), len(dropped))
    db.finish_resume(conn, user_id, offer_id, made={
        "language": language, "content": resume.public(cv),
        "dropped": dropped, "tex": document, "pdf": pdf,
        "filename": resume.filename(profile, offer, language),
        "model": model})
