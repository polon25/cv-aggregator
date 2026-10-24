"""Where places are, for "within so many kilometres of a city": locate()
gives a town's or village's coordinates (Polish ones first), from the
database (table `places`) or, the first time, from OpenStreetMap's
Nominatim, as its usage policy asks: at most a request a second, saying
who asks, every answer kept (also "not found"). km() is the distance
between two points.
"""

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import candidate
import portals

log = logging.getLogger("geo")

URL = "https://nominatim.openstreetmap.org/search"
PAUSE = 1.1
# Names that aren't places.
NOWHERE = {"remote", "zdalnie", "praca zdalna", "zdalna", "polska",
           "poland", "cala polska", "anywhere", "europe", "europa", "emea",
           "worldwide", "hybrid", "hybrydowo"}
_lock = threading.Lock()
_last = [0.0]


def key(name):
    return candidate.normalize(name)


def km(a, b):
    """Kilometres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) \
        * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _ask(name, poland):
    with _lock:
        wait = _last[0] + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    # only towns and villages: not a Warsaw street called after Lisbon
    query = {"q": name, "format": "jsonv2", "limit": 1,
             "accept-language": "pl", "featureType": "settlement"}
    if poland:
        query["countrycodes"] = "pl"
    request = urllib.request.Request(
        URL + "?" + urllib.parse.urlencode(query),
        headers={"User-Agent": portals.USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        found = json.loads(response.read().decode("utf-8"))
    return (float(found[0]["lat"]), float(found[0]["lon"])) if found else None


def locate(conn, names, budget=0):
    """{name: (lat, lon) or None} for the names: from the database, and
    for at most `budget` names it doesn't know yet, from Nominatim (Polish
    places first). Names it couldn't ask about are left out."""
    out, missing = {}, []
    for name in dict.fromkeys(n for n in names if n):
        k = key(name)
        if not k or k in NOWHERE:
            continue
        row = conn.execute("SELECT lat, lon FROM places WHERE key = ?",
                           (k,)).fetchone()
        if row:
            out[name] = (row["lat"], row["lon"]) if row["lat"] is not None \
                else None
        else:
            missing.append(name)
    for name in missing[:budget]:
        try:
            point = _ask(name, True) or _ask(name, False)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            log.info("No place for %r: %s", name, e)
            continue
        with conn:
            conn.execute("INSERT OR REPLACE INTO places VALUES (?, ?, ?, ?, ?)",
                         (key(name), name, point[0] if point else None,
                          point[1] if point else None, time.time()))
        out[name] = point
        log.info("Place %r: %s", name, point)
    return out
