"""Looking for job offers, by one background thread. For each user whose
offers weren't looked for in OFFERS_INTERVAL hours (or who asked for it):

1. search: the portals they chose are searched for each of their terms
   (matching.terms); a portal searched for a term less than
   OFFERS_INTERVAL hours ago, for anyone, isn't asked again: offers are
   shared by all users;
2. match: every offer seen lately that isn't ruled out by the user's wishes
   (matching.rule_out) and looks promising enough (matching.prescore) is
   kept for the user, once even when two portals list it; offers kept
   earlier and not rated yet are judged again by the wishes as they are;
3. review: a model rates the most promising ones (agents.review_offer),
   with their full text fetched from the portal, as many a day as the
   user allows (config.OFFERS_DAILY_REVIEWS or their own number), and any
   the user asked to have rated.

status(user_id) says what's going on for a user, for their page.
"""

import datetime
import logging
import threading
import time

import agents
import candidate
import config
import db
import llm
import matching
import portals

log = logging.getLogger("offers")

CHECK_SECONDS = 300
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


def look(conn, user_id):
    profile, row, _ = db.built_profile(conn, user_id)
    wishes = row["wishes"]
    terms = matching.terms(wishes, row["analysis"])
    if not terms:
        db.offers_done(conn, user_id)
        return
    chosen = wishes["portals"] or config.PORTALS
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


def match(conn, user_id, profile, wishes, analysis):
    """Keeps for the user the offers seen lately that may do for them;
    returns how many were added."""
    since = time.time() - config.OFFER_DAYS * 86400
    offers = sorted(db.recent_offers(conn, since),
                    key=lambda o: o["first_seen"])
    skills = matching.skill_names(profile, wishes)
    matches = {m["offer_id"]: m for m in db.user_matches(conn, user_id)}
    kept, gone, rescored = [], [], []
    for m in matches.values():
        if m["status"] != "new" or m["reviewed_at"] or m["wanted"]:
            kept.append(m)
            continue
        score = matching.prescore(m, wishes, skills, analysis)
        if matching.rule_out(m, wishes) or score < matching.MIN_PRESCORE:
            gone.append(m["offer_id"])
        else:
            kept.append(m)
            rescored.append((m["offer_id"], score))
    added = []
    for offer in offers:
        if offer["id"] in matches or matching.rule_out(offer, wishes):
            continue
        score = matching.prescore(offer, wishes, skills, analysis)
        if score < matching.MIN_PRESCORE or any(
                matching.same_offer(offer, other) for other in kept):
            continue
        kept.append(offer)
        added.append((offer["id"], score))
    db.drop_matches(conn, user_id, gone)
    db.rescore_matches(conn, user_id, rescored)
    db.add_matches(conn, user_id, added)
    log.info("User %d: %d offers kept, %d dropped (of %d seen lately)",
             user_id, len(added), len(gone), len(offers))
    return len(added)


def _midnight():
    today = datetime.date.today()
    return time.mktime(today.timetuple())


def review(conn, user_id, profile, row):
    """Has a model rate the user's offers due for it."""
    account = db.account(conn, user_id)
    if not account["key"] or not (account["use_paid"] or
                                  account["use_free"]):
        return
    left = db.daily_reviews(conn, user_id) - db.reviews_since(
        conn, user_id, _midnight())
    since = time.time() - config.OFFER_DAYS * 86400
    queue = db.to_review(conn, user_id, since, left)
    if not queue:
        return
    profile_text = candidate.text(profile, row["about"])
    wishes_text = matching.describe(row["wishes"], row["goals"])
    for n, offer in enumerate(queue, 1):
        _set_status(user_id, "reviewing", n, len(queue))
        text = db.offer_details(conn, offer["offer_id"])
        if text is None:
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
