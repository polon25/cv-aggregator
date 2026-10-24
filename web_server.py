"""The app's web server, standard library only. Every /api request and
reply is JSON (but uploads and downloads of CV files); errors are
{"error": message}.

    POST /api/register         {username, password}: creates an account and
                               logs in (sets the login cookie); unless
                               ALLOW_REGISTRATION is off
    POST /api/login            {username, password}
    POST /api/logout
    GET  /api/me               the user, whether they can have models read
                               CVs (a key, and some models allowed) (401 if
                               logged out)
    GET  /api/settings         the user's OpenRouter key (whether there is
                               one and its last characters, never the key),
                               its funds, the user's lists of paid and
                               free models, whether the paid ones come
                               first, and the defaults
    POST /api/settings/key     {key}: checks the key with OpenRouter and
                               keeps it; an empty one removes it
    POST /api/settings/models  {paid, free}: the user's own lists of
                               models; {reset: true}: the defaults again
    POST /api/settings/use     {use_paid, use_free} (either): whether the
                               paid models may be used, and the free ones
    POST /api/settings/reviews {daily}: offers a model rates a day
    GET  /api/models           OpenRouter's models with their prices
    GET  /api/schema           the profile's fields and sections
    GET  /api/profile          the user's CVs, their merged profile, what
                               the program counted, their description, the
                               analysis and whether it's still up to date
    POST /api/profile/about    {about}: the user's own description
    POST /api/profile/personal {field: value}: personal details; a value
                               null goes back to what the CVs say
    POST /api/profile/items/<section>
                               {id, fields}: the user's version of an item
                               (no id: a new one)
    POST /api/profile/items/<section>/<id>/delete
    POST /api/profile/items/<section>/<id>/revert
                               drops the user's changes to an item
    POST /api/profile/removed/<section>/<id>/restore
                               brings back what was deleted
    POST /api/profile/analysis has a new analysis written
    GET  /api/offers           what the user looks for (and the form's
                               choices), what's searched for, whether a
                               search is going on, the day's ratings, and
                               the offers kept for them with their reviews
    POST /api/offers/wishes    {wishes, goals}: what the user looks for;
                               offers are looked for again
    POST /api/offers/search    has offers looked for now
    POST /api/offers/<id>/status
                               {status}: new, interested, applied, rejected
    POST /api/offers/<id>/review
                               has a model rate the offer (beyond the day's
                               number)
    POST /api/cvs?name=<file name>
                               the file as the request body: uploads a CV,
                               which is then read in the background
    GET  /api/cvs/<id>/file    the file as uploaded
    GET  /api/cvs/<id>/text    {text}: the text read from it
    POST /api/cvs/<id>/delete
    POST /api/cvs/<id>/retry   reads a CV that failed again
    anything else              the static page in web/
"""

import json
import logging
import re
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import candidate
import config
import db
import documents
import llm
import matching
import offers
import worker

log = logging.getLogger("web")

COOKIE = "login"
MAX_BODY = 64 * 1024


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


MODEL_NAME = re.compile(r"~?[\w.\-]+/[\w.\-:]+")


def _settings_json(conn, user):
    """The user's key (never the key itself) and ladder of models."""
    account = db.account(conn, user["id"])
    key = account["key"]
    return {
        "key": {"set": bool(key), "hint": key[-4:] if key else ""},
        "funds": llm.funds_status(account["key"]) if account["key"]
        else None,
        "paid": account["paid"], "free": account["free"],
        "custom": account["custom"], "use_paid": account["use_paid"],
        "use_free": account["use_free"],
        "daily_reviews": db.daily_reviews(conn, user["id"]),
        "max_reviews": config.OFFERS_MAX_REVIEWS,
        "defaults": {"paid": config.OPENROUTER_PAID_MODELS,
                     "free": config.OPENROUTER_MODELS},
        "last": config.OPENROUTER_MODEL, "max": db.MAX_MODELS,
        "low_funds": config.OPENROUTER_LOW_FUNDS,
    }


def _clean_models(body):
    """The two lists of models from the settings form, checked against
    OpenRouter's list of models (when it can be had)."""
    known = {m["id"]: m for m in llm.catalogue() or []}
    lists = {}
    for which, label in (("paid", "płatnych"), ("free", "darmowych")):
        names = body.get(which)
        if not isinstance(names, list):
            raise db.Error("Nieprawidłowa lista modeli.")
        clean = []
        for name in names:
            name = str(name).strip()
            if not name or name in clean:
                continue
            if len(name) > db.MAX_MODEL_NAME or \
                    not MODEL_NAME.fullmatch(name):
                raise db.Error("„%s” nie wygląda na nazwę modelu (np. "
                               "google/gemma-4-31b-it:free)." % name[:60])
            if name == config.OPENROUTER_MODEL:
                raise db.Error("%s jest zawsze ostatni na liście – nie "
                               "trzeba go dodawać." % name)
            if known and name not in known:
                raise db.Error("OpenRouter nie ma modelu „%s”." % name)
            if known and which == "free" and not known[name]["free"]:
                raise db.Error("Model %s jest płatny: dodaj go do listy "
                               "płatnych." % name)
            clean.append(name)
        if len(clean) > db.MAX_MODELS:
            raise db.Error("Najwyżej %d modeli na liście %s."
                           % (db.MAX_MODELS, label))
        lists[which] = clean
    if set(lists["paid"]) & set(lists["free"]):
        raise db.Error("Ten sam model nie może być na obu listach.")
    return lists["paid"], lists["free"]


def _cv_json(cv):
    return {"id": cv["id"], "filename": cv["filename"],
            "kind": documents.KINDS.get(cv["kind"], cv["kind"]),
            "size": cv["size"], "uploaded_at": cv["uploaded_at"],
            "language": cv["language"], "status": cv["status"],
            "error": cv["error"], "chars": cv["chars"],
            "model": (cv["model"] or {}).get("model"),
            "processed_at": cv["processed_at"],
            "found": {s: len(cv["extraction"][s])
                      for s in candidate.FIELDS}
            if cv["extraction"] else None}


def _profile_json(conn, user):
    profile, row, cvs = db.built_profile(conn, user["id"])
    text = db.profile_text(profile, row)
    stats = candidate.stats(profile)
    stats["experience"] = candidate.years_months(stats["experience_months"])
    status = row["analysis_status"]
    return {
        "cvs": [_cv_json(c) for c in cvs], "max_cvs": config.MAX_CVS,
        "max_mb": config.MAX_CV_MB,
        "profile": candidate.public(profile), "stats": stats,
        "about": row["about"],
        "analysis": {
            "status": status, "error": row["analysis_error"],
            "result": row["analysis"], "at": row["analysis_at"],
            "model": (row["analysis_model"] or {}).get("model"),
            "stale": bool(row["analysis"]) and
            row["analysis_of"] != candidate.fingerprint(text),
        },
        "empty": not text,
        "busy": any(c["status"] in ("pending", "working") for c in cvs) or
        status in ("pending", "working"),
    }


MATCH_STATUSES = ("new", "interested", "applied", "rejected")
MATCH_FIELDS = ("offer_id", "portal", "url", "title", "company", "cities",
                "mode", "salary_min", "salary_max", "currency", "contracts",
                "level", "skills", "nice", "posted", "first_seen",
                "last_seen", "prescore", "status", "wanted", "score",
                "review", "review_error", "reviewed_at", "created_at")


def _offers_json(conn, user):
    profile, row, _ = db.built_profile(conn, user["id"])
    skills = matching.skill_names(profile, row["wishes"])
    matches = []
    for m in db.user_matches(conn, user["id"]):
        item = {k: m.get(k) for k in MATCH_FIELDS}
        item["wanted"] = bool(m["wanted"])
        item["model"] = (m["model"] or {}).get("model")
        item["have"] = [s for s in m["skills"]
                        if candidate.normalize(s) in skills]
        matches.append(item)
    midnight = offers._midnight()
    paths = (row["analysis"] or {}).get("career_paths") or []
    return {
        "wishes": row["wishes"], "goals": row["goals"],
        "choices": matching.schema(),
        "terms": matching.terms(row["wishes"], row["analysis"]),
        "suggestions": [{"title": p["title"], "fit": p["fit"],
                         "roles": p["roles"], "keywords": p["keywords"]}
                        for p in paths],
        "status": offers.status(user["id"]),
        "offers_at": row["offers_at"], "wanted": bool(row["offers_wanted"]),
        "interval": config.OFFERS_INTERVAL,
        "daily": {"limit": db.daily_reviews(conn, user["id"]),
                  "used": db.reviews_since(conn, user["id"], midnight)},
        "fresh": time.time() - 2 * 86400,
        "matches": matches,
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config.WEB_DIR, **kwargs)

    def log_message(self, format, *args):
        log.debug("%s - %s", self.address_string(), format % args)

    def end_headers(self):
        # The page's files change with updates; always check for new ones.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # --- dispatch -----------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            return self._api("GET", path)
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/"):
            return self._api("POST", path)
        self.send_error(405)

    def _api(self, method, path):
        self._cookie = None
        self._sent = False
        conn = db.connect()
        try:
            data = self._route(conn, method, path)
            status = 200
        except db.Error as e:
            data, status = {"error": str(e)}, 400
        except HttpError as e:
            data, status = {"error": str(e)}, e.status
        except Exception:
            log.exception("%s %s failed", method, path)
            data, status = {"error": "Błąd serwera."}, 500
        finally:
            conn.close()
        if self._sent:
            return
        try:
            self._json(data, status)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page went away

    def _route(self, conn, method, path):
        if method == "POST" and path == "/api/register":
            if not config.ALLOW_REGISTRATION:
                raise HttpError(403, "Zakładanie kont jest wyłączone: "
                                     "poproś administratora o konto.")
            body = self._body()
            user = db.create_user(conn, body.get("username"),
                                  body.get("password"))
            self._cookie = db.create_login(conn, user["id"])
            return {"user": user}
        if method == "POST" and path == "/api/login":
            body = self._body()
            user = db.check_login(conn, body.get("username"),
                                  body.get("password"))
            if not user:
                time.sleep(1)  # slows down password guessing
                raise HttpError(401, "Nieprawidłowa nazwa użytkownika "
                                     "lub hasło.")
            self._cookie = db.create_login(conn, user["id"])
            return {"user": user}
        if method == "POST" and path == "/api/logout":
            db.delete_login(conn, self._token())
            self._cookie = ""
            return {}
        if method == "GET" and path == "/api/schema":
            return dict(candidate.schema(),
                        registration=config.ALLOW_REGISTRATION)

        user = db.login_user(conn, self._token())
        if not user:
            raise HttpError(401, "Zaloguj się.")
        if method == "GET" and path == "/api/me":
            account = db.account(conn, user["id"])
            return {"user": user, "has_key": bool(account["key"]),
                    "has_models": account["use_paid"] or
                    account["use_free"]}
        if path == "/api/settings" or path.startswith("/api/settings/"):
            return self._settings(conn, user, method, path[14:])
        if method == "GET" and path == "/api/models":
            return {"models": llm.catalogue() or []}
        if path == "/api/profile" or path.startswith("/api/profile/"):
            return self._profile(conn, user, method, path[13:])
        if path == "/api/offers" or path.startswith("/api/offers/"):
            return self._offers(conn, user, method, path[12:])
        if method == "POST" and path == "/api/cvs":
            return self._upload(conn, user)
        m = re.fullmatch(r"/api/cvs/(\d+)/([a-z]+)", path)
        if m:
            cv = db.get_cv(conn, user["id"], int(m.group(1)))
            if not cv:
                raise HttpError(404, "Nie ma takiego CV.")
            return self._cv(conn, user, cv, method, m.group(2))
        raise HttpError(404, "Nie ma takiej strony.")

    # --- account ------------------------------------------------------

    def _settings(self, conn, user, method, action):
        if method == "GET" and not action:
            return _settings_json(conn, user)
        if method != "POST":
            raise HttpError(405, "Niedozwolona metoda.")
        body = self._body()
        if action == "key":
            key = "".join(str(body.get("key") or "").split())
            if not key:
                db.set_api_key(conn, user["id"], None)
                log.info("%s removed their API key", user["username"])
                return _settings_json(conn, user)
            if len(key) > 300 or not key.isascii():
                raise db.Error("To nie wygląda na klucz API.")
            try:
                llm.check_key(key)
            except llm.BadKey:
                raise db.Error("OpenRouter nie rozpoznaje tego klucza. "
                               "Sprawdź, czy wkleiłaś/eś go w całości.")
            except llm.LLMError as e:
                log.warning("A key couldn't be checked: %s", e)
                raise db.Error("Nie udało się teraz sprawdzić klucza "
                               "w OpenRouterze. Spróbuj ponownie za chwilę.")
            db.set_api_key(conn, user["id"], key)
            log.info("%s set their API key", user["username"])
            return _settings_json(conn, user)
        if action == "models":
            if body.get("reset"):
                db.set_models(conn, user["id"], None, None)
            else:
                db.set_models(conn, user["id"], *_clean_models(body))
            return _settings_json(conn, user)
        if action == "reviews":
            try:
                daily = int(body.get("daily"))
            except (TypeError, ValueError):
                daily = -1
            if not 0 <= daily <= config.OFFERS_MAX_REVIEWS:
                raise db.Error("Liczba ocen dziennie: od 0 do %d."
                               % config.OFFERS_MAX_REVIEWS)
            db.set_daily_reviews(conn, user["id"], daily)
            return _settings_json(conn, user)
        if action == "use":
            for which in ("paid", "free"):
                if "use_" + which in body:
                    db.set_use(conn, user["id"], which,
                               body["use_" + which] is True)
            if "use_free" in body:
                log.info("%s turned free models %s", user["username"],
                         "on" if body["use_free"] is True else "off")
            return _settings_json(conn, user)
        raise HttpError(404, "Nie ma takiej strony.")

    # --- the profile --------------------------------------------------

    def _profile(self, conn, user, method, action):
        if method == "GET" and not action:
            return _profile_json(conn, user)
        if method != "POST":
            raise HttpError(405, "Niedozwolona metoda.")
        body = self._body()
        if action == "about":
            db.save_about(conn, user["id"], body.get("about"))
            return _profile_json(conn, user)
        if action == "analysis":
            profile, row, _ = db.built_profile(conn, user["id"])
            if not db.profile_text(profile, row):
                raise db.Error("Profil jest pusty: wgraj CV albo uzupełnij "
                               "go ręcznie.")
            if not db.account(conn, user["id"])["key"]:
                raise db.Error(worker.NO_KEY)
            db.request_analysis(conn, user["id"])
            worker.wake()
            return _profile_json(conn, user)
        profile, row, _ = db.built_profile(conn, user["id"])
        overrides = row["overrides"]
        if action == "personal":
            candidate.set_personal(overrides, body)
            db.save_overrides(conn, user["id"], overrides)
            return _profile_json(conn, user)
        m = re.fullmatch(r"(items|removed)/([a-z]+)(?:/([a-z0-9\-]+)/"
                         r"([a-z]+))?", action)
        if not m or m.group(2) not in candidate.FIELDS:
            raise HttpError(404, "Nie ma takiej strony.")
        kind, section, item_id, verb = m.groups()
        try:
            if kind == "items" and not verb:
                fields = body.get("fields")
                if not isinstance(fields, dict):
                    raise HttpError(400, "Nieprawidłowe dane.")
                candidate.save_item(overrides, profile, section,
                                    body.get("id"), fields)
            elif kind == "items" and verb == "delete":
                candidate.remove_item(overrides, profile, section, item_id)
            elif kind == "items" and verb == "revert":
                candidate.revert_item(overrides, section, item_id)
            elif kind == "removed" and verb == "restore":
                candidate.restore_item(overrides, section, item_id)
            else:
                raise HttpError(404, "Nie ma takiej strony.")
        except KeyError:
            raise db.Error("Tego wpisu już nie ma – odśwież stronę.")
        except ValueError as e:
            if str(e) == "too many":
                raise db.Error("Za dużo wpisów w tej części profilu.")
            raise db.Error("Wypełnij pole „%s”." % e)
        db.save_overrides(conn, user["id"], overrides)
        return _profile_json(conn, user)

    # --- offers ---------------------------------------------------------

    def _offers(self, conn, user, method, action):
        if method == "GET" and not action:
            return _offers_json(conn, user)
        if method != "POST":
            raise HttpError(405, "Niedozwolona metoda.")
        body = self._body()
        if action == "wishes":
            db.save_wishes(conn, user["id"], matching.clean(body.get("wishes")),
                           body.get("goals"))
            offers.wake()
            return _offers_json(conn, user)
        if action == "search":
            db.want_offers(conn, user["id"])
            offers.wake()
            return _offers_json(conn, user)
        m = re.fullmatch(r"(\d+)/(status|review)", action)
        if not m or not db.get_match(conn, user["id"], int(m.group(1))):
            raise HttpError(404, "Nie ma takiej oferty.")
        offer_id = int(m.group(1))
        if m.group(2) == "status":
            if body.get("status") not in MATCH_STATUSES:
                raise HttpError(400, "Nieprawidłowy stan.")
            db.set_match_status(conn, user["id"], offer_id, body["status"])
        else:
            account = db.account(conn, user["id"])
            if not account["key"]:
                raise db.Error(worker.NO_KEY)
            if not (account["use_paid"] or account["use_free"]):
                raise db.Error(worker.NO_MODELS)
            db.want_review(conn, user["id"], offer_id)
            offers.wake()
        return _offers_json(conn, user)

    # --- CVs ------------------------------------------------------------

    def _upload(self, conn, user):
        length = int(self.headers.get("Content-Length") or 0)
        if length > config.MAX_CV_MB * 1024 * 1024:
            raise HttpError(413, "Plik jest za duży (najwyżej %g MB)."
                            % config.MAX_CV_MB)
        if not length:
            raise db.Error("Plik jest pusty.")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = (query.get("name") or ["cv"])[0]
        data = self.rfile.read(length)
        kind = documents.kind_of(data, name)
        if not kind:
            raise db.Error("Nieobsługiwany rodzaj pliku: wgraj PDF, DOCX, "
                           "ODT, DOC, RTF albo TXT.")
        cv_id = db.add_cv(conn, user["id"], name, kind, data)
        log.info("%s uploaded CV %d (%s, %d bytes)", user["username"], cv_id,
                 kind, length)
        worker.wake()
        return _profile_json(conn, user)

    def _cv(self, conn, user, cv, method, action):
        if method == "GET" and action == "file":
            with open(db.cv_path(cv), "rb") as f:
                data = f.read()
            self._file(data, cv["filename"])
            return None
        if method == "GET" and action == "text":
            return {"text": cv["text"] or ""}
        if method != "POST":
            raise HttpError(405, "Niedozwolona metoda.")
        if action == "delete":
            db.delete_cv(conn, cv)
            log.info("%s deleted CV %d", user["username"], cv["id"])
            if any(c["extraction"] for c in db.user_cvs(conn, user["id"])):
                db.request_analysis(conn, user["id"])
                worker.wake()
            return _profile_json(conn, user)
        if action == "retry":
            if cv["status"] != "failed":
                raise db.Error("To CV nie czeka na ponowne odczytanie.")
            db.retry_cv(conn, cv)
            worker.wake()
            return _profile_json(conn, user)
        raise HttpError(404, "Nie ma takiej strony.")

    # --- plumbing -------------------------------------------------------

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(413, "Za duże zapytanie.")
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise HttpError(415, "Oczekiwano JSON.")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HttpError(400, "Nieprawidłowy JSON.")
        if not isinstance(body, dict):
            raise HttpError(400, "Nieprawidłowy JSON.")
        return body

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        return cookie[COOKIE].value if COOKIE in cookie else None

    def _file(self, data, filename):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''"
                         + urllib.parse.quote(filename))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        self._sent = True

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self._cookie is not None:
            max_age = db.LOGIN_DAYS * 86400 if self._cookie else 0
            self.send_header(
                "Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; "
                "SameSite=Lax" % (COOKIE, self._cookie, max_age))
        self.end_headers()
        self.wfile.write(body)


def serve():
    server = ThreadingHTTPServer((config.WEB_HOST, config.WEB_PORT), Handler)
    log.info("Serving on http://%s:%d", config.WEB_HOST, config.WEB_PORT)
    server.serve_forever()
