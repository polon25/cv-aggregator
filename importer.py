"""Pages users send from their browser (the "Do CV Aggregatora" bookmarklet,
web/offers.js), for portals the app can't read itself (pracuj.pl,
LinkedIn; portals.imported()) or any other: what the browser shows is
read here, by the offers thread (offers.py).

A page comes as {url, title, text (what it shows), ld (its JSON-LD
JobPostings), cards ([{url, text}]: the tiles around links to offers, by
the portal's "offer" pattern)}:

- a results page (tiles): a model reads the tiles' offers
  (agents.read_cards; offers kept already, from an earlier page, aren't
  read again); they're sifted by the user's wishes like any
  others (offers.judge), the ones that don't do are left out, and the
  RATE_LIST most promising are rated at once (from their tiles alone);
- an offer's page (its address is an offer's, by the portal's "offer"
  pattern, or, on a site no portal describes, it has a JobPosting; its
  other tiles, "similar offers", are left out), or one the portal shows
  beside the list (LinkedIn's currentJobId): a model reads it, with its
  full text (agents.read_page), and it's rated at once, whatever it
  misses.

Neither counts in the day's number of ratings.

Job alerts that come by mail (alerts.py) are such results pages too
("mail": true), but their offers aren't rated at once: they wait for the
day's ratings, like those of the portals the app searches.
"""

import logging
import re

import agents
import db
import llm
import matching
import offers
import portals
import worker

log = logging.getLogger("importer")

MAX_CARDS = 60
CARD_CHARS = 600
PAGE_CHARS = 20000
# Offers of a results page rated at once, the most promising first.
RATE_LIST = 20


def clean(payload):
    """The page as the browser sent it, cut to size; None if it's no
    page."""
    if not isinstance(payload, dict) or not str(payload.get("url") or "") \
            .startswith(("http://", "https://")):
        return None
    cards = []
    for card in payload.get("cards") or []:
        if isinstance(card, dict) and str(card.get("url") or "").startswith(
                ("http://", "https://")) and card.get("text"):
            cards.append({"url": str(card["url"])[:500],
                          "text": str(card["text"])[:CARD_CHARS]})
    postings = [p for p in payload.get("ld") or []
                if isinstance(p, dict) and p.get("@type") == "JobPosting"]
    return {"url": str(payload["url"])[:2000],
            "title": str(payload.get("title") or "")[:300],
            "text": str(payload.get("text") or "")[:PAGE_CHARS],
            "ld": postings[:1], "cards": cards[:MAX_CARDS * 2]}


def process(conn, item):
    """Reads an import (db.claim_import()'s) and keeps its offers for its
    user; returns the result: {offers: ids, tiles, kept, out, single}."""
    page = item["payload"]
    account = db.account(conn, item["user_id"])
    if not account["key"]:
        raise ImportFailed(worker.NO_KEY)
    if not (account["use_paid"] or account["use_free"]):
        raise ImportFailed(worker.NO_MODELS)
    portal, key, address = portals.identify(page["url"])
    known = portals.PORTALS.get(portal, {}).get("import")
    # an offer's own page: that offer alone (not the "similar offers"
    # around it); on a portal no file describes, a page with a JobPosting
    offer_page = bool(re.search(known["offer"], page["url"])) if known and \
        known.get("offer") else bool(page["ld"])
    # or one open beside a list (LinkedIn's currentJobId)
    single = key if offer_page or (known and key) else None
    tiles, seen = [], {single}
    for card in page["cards"] if not offer_page else []:
        card_portal, card_key, card_url = portals.identify(card["url"])
        if card_key and card_key not in seen:
            seen.add(card_key)
            tiles.append((card_portal, card_key, card_url, card["text"]))
    tiles = tiles[:MAX_CARDS]
    if not tiles and not single and not page.get("mail"):
        single = key  # a page of a site no portal describes: one offer
    # tiles of offers kept already aren't read again
    known_ids = db.offer_ids(conn, [(t[0], t[1]) for t in tiles])
    known_tiles = [t for t in tiles if (t[0], t[1]) in known_ids]
    tiles = [t for t in tiles if (t[0], t[1]) not in known_ids]
    found = []
    try:
        if tiles:
            read, _ = agents.read_cards([t[3] for t in tiles], account)
            for n, offer in read.items():
                offer.update(portal=tiles[n][0], key=tiles[n][1],
                             url=tiles[n][2])
                found.append(portals.tidy(offer))
        if single:
            selected = single if tiles else None
            offer, _ = agents.read_page(page["text"], page["title"],
                                        (page["ld"] or [None])[0], selected,
                                        account)
            offer.update(portal=portal, key=single, url=address)
            found.append(portals.tidy(offer))
    except llm.LLMError as e:
        log.warning("Import %d not read: %s", item["id"], e)
        raise ImportFailed(worker.FAILED if account["use_free"]
                           else worker.PAID_FAILED)
    if not found and not known_tiles:
        raise ImportFailed("Nie znalazłam na tej stronie ofert pracy.")
    db.save_offers(conn, found)
    db.seen_offers(conn, list(known_ids.values()))
    ids = db.offer_ids(conn, [(o["portal"], o["key"]) for o in found])
    ids.update(known_ids)
    rows = db.offers_by_id(conn, list(ids.values()))
    judged, row = offers.judge(conn, item["user_id"], rows)
    kept, out = [], 0
    for offer, ruled, score, unmet, penalty in judged:
        is_single = offer["key"] == single and offer["portal"] == portal
        if ruled and not is_single:
            out += 1
            continue
        if ruled:  # sent on purpose: kept, with what rules it out
            hard = [(r, 100) for r, points in matching.reasons(
                offer, row["wishes"]) if points is None]
            unmet, penalty = hard + unmet, penalty + 100
        kept.append((offer, score, unmet, penalty, is_single))
    db.keep_matches(conn, item["user_id"], [
        (o["id"], score, unmet, penalty) for o, score, unmet, penalty, _
        in kept])
    # the offer's page is rated at once (again, if it was, now with its
    # text), the best of a list unless rated since the wishes changed
    rate = [o["id"] for o, _, _, _, one in kept if one]
    db.want_reviews(conn, item["user_id"], rate)
    # (an alert's offers wait for the day's ratings: nobody asked)
    best = [] if page.get("mail") else [o["id"] for o, *_ in sorted(
        (k for k in kept if not k[4]), key=lambda k: -k[1])][:RATE_LIST]
    db.want_reviews(conn, item["user_id"], best, row["wishes_at"] or 0)
    rate += best
    log.info("Import %d (%s): %d tiles (%d known), %d offers read, %d kept, "
             "%d rated", item["id"], portal, len(tiles) + len(known_tiles),
             len(known_tiles), len(found), len(kept), len(rate))
    return {"offers": [o["id"] for o, *_ in kept],
            "tiles": len(tiles) + len(known_tiles),
            "read": len(found) + len(known_tiles), "kept": len(kept),
            "out": out, "single": bool(single)}


class ImportFailed(Exception):
    """An import that can't be done, with a message for the user."""
