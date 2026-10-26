"""Looking for job offers, by one background thread. For each user whose
offers weren't looked for in OFFERS_INTERVAL hours (or who asked for it):

1. search: the portals they chose are searched for each of their terms
   (matching.terms); a portal searched for a term less than
   OFFERS_INTERVAL hours ago, for anyone, isn't asked again: offers are
   shared by all users;
2. match: every offer seen lately that isn't ruled out by the user's wishes
   (matching.judge; places within the user's distance of their cities, by
   geo.py) and looks promising enough (matching.prescore, less the points
   of the wishes it doesn't meet) is kept for the user, once even when
   two portals list it; offers kept earlier and still unmarked are judged
   again by the wishes as they are (rated ones that no longer do go too);
3. review: a model rates how the most promising ones not rated since the
   wishes last changed fit the candidate (agents.review_offer; the
   points for unmet wishes are taken off its score when it's shown),
   with their full text fetched from the portal, as many a day as the
   user allows (config.OFFERS_DAILY_REVIEWS or their own number; counted
   anew when the wishes change), and any the user asked to have
   rated.

Before all that, job alerts that came to users' mailboxes are read
(alerts.py) and, with the pages users send from their browser, made
offers (importer.py).

status(user_id) says what's going on for a user, for their page.
"""

import datetime
import logging
import threading
import time

import imaplib

import agents
import alerts
import candidate
import config
import db
import geo
import importer
import llm
import matching
import portals
import worker

log = logging.getLogger("offers")

CHECK_SECONDS = 300
# Seconds before a mailbox that couldn't be read is tried again.
MAIL_RETRY = 120
# A job alert that no model could read is tried again this many times,
# after this many seconds more each time.
ALERT_ATTEMPTS = 5
ALERT_PAUSE = 1800
# Places looked up on Nominatim at most per user and search.
GEOCODE_BUDGET = 40
_wake = threading.Event()
_status = {}  # user id -> {state, done, total}
_lock = threading.Lock()


def wake():
    _wake.set()


def status(user_id):
    with _lock:
        return dict(_status[user_id]) if user_id in _status else None


def _set_status(user_id, state, done=0, total=0):
    with _lock:
        if state:
            _status[user_id] = {"state": state, "done": done, "total": total}
        else:
            _status.pop(user_id, None)


def start():
    threading.Thread(target=_run, name="offers", daemon=True).start()


def _run():
    while True:
        try:
            _once()
        except Exception:
            log.exception("Looking for offers failed")
        _wake.wait(CHECK_SECONDS)
        _wake.clear()


def _once():
    conn = db.connect()
    try:
        _read_mail(conn)
        while _import_one(conn):
            pass
        now = time.time()
        db.forget_offers(conn, now - 2 * config.OFFER_DAYS * 86400)
        for user_id in db.users_due(conn,
                                    now - config.OFFERS_INTERVAL * 3600):
            try:
                look(conn, user_id)
            finally:
                _set_status(user_id, None)
        for user_id in db.users_wanting_reviews(conn):
            profile, row, _ = db.built_profile(conn, user_id)
            try:
                review(conn, user_id, profile, row)
            finally:
                _set_status(user_id, None)
    finally:
        conn.close()


def _read_mail(conn):
    """Reads the job alerts that came to users' mailboxes since the last
    time (alerts.py), each user's every config.MAIL_INTERVAL minutes; each
    alert becomes an import. Alerts read and kept before go to the trash
    (unless the user keeps them). A mailbox that couldn't be read (e.g. the
    network not up yet after the server started) is tried again in
    MAIL_RETRY seconds, not after the whole interval."""
    now = time.time()
    for user_id in db.mailbox_users(conn):
        box, password = db.mailbox(conn, user_id)
        if not box or not password or \
                now - (box.get("read_at") or 0) < config.MAIL_INTERVAL * 60 \
                or now - (box.get("tried_at") or 0) < MAIL_RETRY:
            continue
        box["tried_at"] = now
        try:
            pages, box = alerts.collect(box, password)
            box.pop("error", None)
            box["read_at"] = now
        except (alerts.AlertsError, imaplib.IMAP4.error, OSError) as e:
            log.warning("Mailbox of user %d not read: %s", user_id, e)
            box["error"] = str(e)[:300]
            pages = []
        for page in pages:
            db.add_import(conn, user_id, page)
        if box.get("trash", True) and not box.get("error"):
            done = db.alerts_to_trash(conn, user_id, box.get("uidvalidity"))
            try:
                moved = alerts.trash(box, password, [uid for _, uid in done])
                db.alerts_trashed(conn, [i for i, _ in done])
                if moved:
                    log.info("User %d: %d job alerts moved to the trash",
                             user_id, moved)
            except (alerts.AlertsError, imaplib.IMAP4.error, OSError) as e:
                log.warning("Alerts of user %d not trashed: %s", user_id, e)
        if pages:
            box["alerts"] = box.get("alerts", 0) + len(pages)
            box["alert_at"] = time.time()
            log.info("User %d: %d job alerts from their mailbox", user_id,
                     len(pages))
        db.save_mailbox_state(conn, user_id, box)


def _import_one(conn):
    """Reads one page a user sent (importer.py); False when none waits."""
    item = db.claim_import(conn)
    if not item:
        return False
    _set_status(item["user_id"], "importing")
    try:
        db.finish_import(conn, item["id"], importer.process(conn, item))
    except importer.ImportFailed as e:
        # a job alert is read again later: its mail stays till then
        if e.retry and item["payload"].get("mail") and \
                item["attempts"] < ALERT_ATTEMPTS:
            db.retry_import(conn, item["id"],
                            ALERT_PAUSE * (item["attempts"] + 1))
        else:
            db.finish_import(conn, item["id"], error=str(e))
    except Exception:
        log.exception("Import %d failed", item["id"])
        db.finish_import(conn, item["id"], error="Nie udało się odczytać "
                         "strony.")
    finally:
        _set_status(item["user_id"], None)
    # its offers are rated at once
    profile, row, _ = db.built_profile(conn, item["user_id"])
    try:
        review(conn, item["user_id"], profile, row)
    finally:
        _set_status(item["user_id"], None)
    return True


def look(conn, user_id):
    profile, row, _ = db.built_profile(conn, user_id)
    wishes = row["wishes"]
    terms = matching.terms(wishes, row["analysis"])
    if not terms:
        db.offers_done(conn, user_id)
        return
    chosen = chosen_portals(wishes)
    _set_status(user_id, "searching", 0, len(chosen) * len(terms))
    fresh = time.time() - config.OFFERS_INTERVAL * 3600
    done = 0
    for portal in chosen:
        for term in terms:
            done += 1
            _set_status(user_id, "searching", done, len(chosen) * len(terms))
            last = db.searched_at(conn, portal, term)
            if last and last > fresh:
                continue
            try:
                found = portals.search(portal, term)
            except portals.PortalError as e:
                log.warning("Searching %s for %r failed: %s", portal, term, e)
                db.record_search(conn, portal, term, error=str(e)[:300])
                continue
            db.save_offers(conn, found)
            db.record_search(conn, portal, term, found=len(found))
            log.info("%s, %r: %d offers", portal, term, len(found))
    match(conn, user_id, profile, wishes, row["analysis"])
    review(conn, user_id, profile, row)
    db.offers_done(conn, user_id)


def chosen_portals(wishes):
    """The portals the user chose (that are still there), or the default
    ones."""
    return [p for p in wishes["portals"] if p in portals.PORTALS] or \
        portals.defaults()


def match(conn, user_id, profile, wishes, analysis):
    """Keeps for the user the offers seen lately on their portals that may
    do for them; returns how many were added."""
    since = time.time() - config.OFFER_DAYS * 86400
    chosen = set(chosen_portals(wishes))
    offers = sorted((o for o in db.recent_offers(conn, since)
                     if o["portal"] in chosen),
                    key=lambda o: o["first_seen"])
    skills = matching.skill_names(profile, wishes)
    matches = {m["offer_id"]: m for m in db.user_matches(conn, user_id)}
    near = _nearness(conn, wishes, offers + list(matches.values()))
    kept, gone, rescored = [], [], []
    for m in matches.values():
        if m["status"] != "new" or m["wanted"]:
            kept.append(m)
            continue
        if portals.searchable(m["portal"]) and m["portal"] not in chosen:
            gone.append(m["offer_id"])
            continue
        out, penalty, unmet = matching.judge(m, wishes, near(m))
        score = _score(m, wishes, skills, analysis, penalty)
        if out or (score < matching.MIN_PRESCORE and not m["reviewed_at"]):
            gone.append(m["offer_id"])
        else:
            kept.append(m)
            rescored.append((m["offer_id"], score, unmet, penalty))
    added = []
    for offer in offers:
        if offer["id"] in matches:
            continue
        out, penalty, unmet = matching.judge(offer, wishes, near(offer))
        if out:
            continue
        score = _score(offer, wishes, skills, analysis, penalty)
        if score < matching.MIN_PRESCORE or any(
                matching.same_offer(offer, other) for other in kept):
            continue
        kept.append(offer)
        added.append((offer["id"], score, unmet, penalty))
    db.drop_matches(conn, user_id, gone)
    db.rescore_matches(conn, user_id, rescored)
    db.add_matches(conn, user_id, added)
    log.info("User %d: %d offers kept, %d dropped (of %d seen lately)",
             user_id, len(added), len(gone), len(offers))
    return len(added)


def judge(conn, user_id, offers):
    """The offers judged by the user's wishes: ([(offer, ruled out,
    prescore less the points of unmet wishes, unmet, penalty)], the
    user's profiles row)."""
    profile, row, _ = db.built_profile(conn, user_id)
    wishes = row["wishes"]
    skills = matching.skill_names(profile, wishes)
    near = _nearness(conn, wishes, offers)
    out = []
    for offer in offers:
        ruled, penalty, unmet = matching.judge(offer, wishes, near(offer))
        out.append((offer, ruled, _score(offer, wishes, skills,
                                         row["analysis"], penalty),
                    unmet, penalty))
    return out, row


def _score(offer, wishes, skills, analysis, penalty):
    """The offer's prescore, less the points of the wishes it doesn't
    meet."""
    return round(matching.prescore(offer, wishes, skills, analysis) -
                 penalty / 100, 3)


def _nearness(conn, wishes, offers):
    """near(offer): whether any of the offer's places is within the user's
    distance of their cities; None when that isn't known (the cities'
    names are compared then, see matching.reasons). Places come from the
    portal, else from the offer's cities' names (geo.locate)."""
    if not wishes["cities"] or not wishes.get("distance"):
        return lambda offer: None
    centres = [p for p in geo.locate(conn, wishes["cities"],
                                     len(wishes["cities"])).values() if p]
    if not centres:
        return lambda offer: None
    unplaced = [c for o in offers if not o.get("points") for c in o["cities"]]
    places = geo.locate(conn, unplaced, GEOCODE_BUDGET)

    def near(offer):
        points = [tuple(p) for p in offer.get("points") or []] or \
            [places[c] for c in offer["cities"] if places.get(c)]
        if not points:
            return None
        return any(geo.km(p, c) <= wishes["distance"]
                   for p in points for c in centres)
    return near


def day_start(row):
    """Since when ratings count in the day's number: midnight, or the
    last change of what the user looks for, if later."""
    midnight = time.mktime(datetime.date.today().timetuple())
    return max(midnight, row["wishes_at"] or 0)


def review(conn, user_id, profile, row):
    """Has a model rate the user's offers due for it."""
    account = db.account(conn, user_id)
    if not account["key"] or not (account["use_paid"] or
                                  account["use_free"]):
        return
    left = max(0, db.daily_reviews(conn, user_id) - db.reviews_since(
        conn, user_id, day_start(row)))
    since = time.time() - config.OFFER_DAYS * 86400
    queue = db.wanted_reviews(conn, user_id) + db.to_review(
        conn, user_id, since, left, row["wishes_at"])
    if not queue:
        return
    profile_text = candidate.text(profile, row["about"])
    # what the program judges by itself the model isn't told
    wishes_text = matching.describe(row["wishes"], row["goals"],
                                    criteria=False)
    for n, offer in enumerate(queue, 1):
        _set_status(user_id, "reviewing", n, len(queue))
        text = db.offer_details(conn, offer["offer_id"])
        if text is None and portals.searchable(offer["portal"]):
            try:
                text = portals.details(offer)
            except portals.PortalError as e:
                log.info("No text for offer %d: %s", offer["offer_id"], e)
                text = ""
            db.save_offer_details(conn, offer["offer_id"], text)
        try:
            result, model = agents.review_offer(profile_text, wishes_text,
                                                offer, text, account)
        except (llm.BadKey, llm.NoModels) as e:
            log.warning("Reviews for user %d stopped: %s", user_id, e)
            return
        except llm.LLMError as e:
            log.warning("Offer %d not reviewed: %s", offer["offer_id"], e)
            db.save_review(conn, user_id, offer["offer_id"],
                           error="Żaden model nie ocenił tej oferty.")
            continue
        db.save_review(conn, user_id, offer["offer_id"], result, model)
    log.info("User %d: %d offers reviewed", user_id, len(queue))
    # advice on what to learn, from what the offers ask, once a day
    if not row["growth_at"] or time.time() - row["growth_at"] > 86400:
        if row["growth_status"] not in ("pending", "working"):
            db.request_growth(conn, user_id)
            worker.wake()
