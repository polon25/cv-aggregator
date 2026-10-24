"""Chat completion requests (OpenRouter by default) for JSON answers, with
retries: ask_json() is how the agents (agents.py) talk to their models.

Requests go with an "account": {key, paid, free, use_paid}, the user's own
key and ladder of models, paid ones and free ones, or the server's key
when the user gave none (db.account). check_key() asks OpenRouter whether
a key is good, funds() how much is left on it for paid models,
catalogue() which models there are and what they cost.

Every request and its outcome (which model answered, how long it took, what
was wrong with an unusable answer) is logged under "llm".
"""

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

import config

log = logging.getLogger("llm")


class LLMError(Exception):
    """A request that failed or an answer that can't be used."""


class RateLimited(LLMError):
    """The model is busy right now (HTTP 429)."""


class TimedOut(LLMError):
    """The model didn't answer in time."""


class BadKey(LLMError):
    """There's no key, or OpenRouter doesn't know it (HTTP 401)."""


class Refused(LLMError):
    """The model can't be had for now: the key's credit or spending limit
    is used up (HTTP 402), the model isn't open to this key (403) or it's
    gone (404)."""


# Seconds to wait before asking a rate-limited model once more.
RATE_LIMIT_PAUSE = 8
# A preferred model still rate-limited after that, or one that didn't
# answer in time, is left out for this long.
RATE_LIMIT_REST = 180
# And one that refused the request (no credit left for a paid model, a
# model that's gone) for this long: the free models answer meanwhile.
REFUSED_REST = 3600
_resting = {}  # (key's mark, model) -> time.monotonic() until it's left out
# Paid models aren't tried with less than this many dollars left.
MIN_FUNDS = 0.01
# Seconds for which what's left is remembered before it's asked again.
FUNDS_TTL = 120
_funds = {}  # key's mark -> {left, at, asking}
_funds_lock = threading.Lock()
# Seconds for which the list of OpenRouter's models is remembered.
CATALOGUE_TTL = 3600
_catalogue = {"models": None, "at": 0.0}
_catalogue_lock = threading.Lock()


def shorten(text, limit=500):
    """Trims long payloads so one bad answer can't flood the log."""
    text = str(text)
    return text if len(text) <= limit else (
        text[:limit] + "... (%d chars)" % len(text))


# How many quotation marks inside strings one answer may get repaired.
MAX_QUOTE_REPAIRS = 80
_NEXT_KEY = re.compile(r'"[A-Za-z_]+"\s*:')


def _decode_lenient(text):
    """Parses the JSON value text starts with, repairing what models
    writing Polish get wrong: a quotation inside a string closed with a
    plain " (opened with „ or ", as in "Ewa: „Nie" i wyszła"), which ends
    the string early, and a string opened with „ instead of ". The parser
    then fails right after the string that ended early: that closing quote
    is escaped and parsing starts over. A comma missing before a key is
    put in too."""
    decoder = json.JSONDecoder(strict=False)
    error = None
    for _ in range(MAX_QUOTE_REPAIRS):
        try:
            return decoder.raw_decode(text)[0]
        except json.JSONDecodeError as e:
            error = error or e
            if e.msg.startswith("Expecting value") and \
                    text[e.pos:e.pos + 1] in ("„", "“", "”"):
                # A string opened with a typographic quote.
                text = text[:e.pos] + '"' + text[e.pos:]
                continue
            if not (e.msg.startswith("Expecting ',' delimiter") or
                    e.msg.startswith("Expecting ':' delimiter")):
                break
            if _NEXT_KEY.match(text, e.pos):
                # Not a quotation: a comma missing before the next key.
                text = text[:e.pos] + "," + text[e.pos:]
                continue
            quote = text.rfind('"', 0, e.pos)
            # Only a quote ending a string can be the culprit: after it
            # comes more of the sentence, not JSON.
            if quote <= 0 or text[quote - 1] == "\\" or \
                    text[quote + 1:e.pos].strip():
                break
            text = text[:quote] + '\\"' + text[quote + 1:]
    raise error


def parse_json_object(text):
    """The first JSON object in text. Models sometimes wrap it in a markdown
    code block or prose, or follow it with more text; strict=False accepts
    raw newlines inside strings, and quotation marks inside strings are
    repaired (see _decode_lenient)."""
    start = text.find("{")
    if start < 0:
        raise LLMError("no JSON object in the answer")
    try:
        parsed = _decode_lenient(text[start:])
    except ValueError as e:
        raise LLMError("invalid JSON: %s" % e)
    if not isinstance(parsed, dict):
        raise LLMError("the JSON isn't an object")
    return parsed


def _mark(key):
    """A name for a key that gives nothing of it away."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _base():
    return config.OPENROUTER_URL.split("/chat/completions")[0]


def _post(payload, key):
    """Sends payload, returns the response body as parsed JSON."""
    request = urllib.request.Request(
        config.OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "X-Title": "CV Aggregator",
        })
    timeout = config.OPENROUTER_TIMEOUT
    started = time.monotonic()
    try:
        # The socket timeout only limits the wait for each next chunk, and
        # OpenRouter keeps sending whitespace while a model is still working,
        # so read the body as it arrives and enforce the timeout on the
        # total.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            chunks = []
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                if time.monotonic() - started > timeout:
                    raise TimedOut("no complete answer within %ds" % timeout)
            status = response.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        error = RateLimited if e.code == 429 else BadKey if e.code == 401 \
            else Refused if e.code in (402, 403, 404) else LLMError
        raise error("HTTP %d: %s" % (e.code, shorten(body)))
    except (urllib.error.URLError, OSError) as e:
        slow = time.monotonic() - started >= timeout
        raise (TimedOut if slow else LLMError)(
            "request failed after %.1fs: %s" % (time.monotonic() - started, e))
    body = b"".join(chunks).decode("utf-8", "replace")
    try:
        return json.loads(body)
    except ValueError:
        raise LLMError("HTTP %d, not JSON: %s" % (status, shorten(body)))


def _get(path, key=None):
    """What OpenRouter says at `path` (its "data")."""
    headers = {"Authorization": "Bearer " + key} if key else {}
    request = urllib.request.Request(_base() + path, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))["data"]


# --- keys and their funds ------------------------------------------------

def _ask_funds(key):
    """Asks OpenRouter what's left on a key: the lesser of the key's own
    limit and its account's credit."""
    left = None
    try:
        about = _get("/auth/key", key)
        if about.get("limit") is not None:
            left = max(0.0, round(float(about["limit_remaining"]), 4))
        credits = _get("/credits", key)
        account = float(credits["total_credits"]) - float(
            credits["total_usage"])
        left = account if left is None else min(left, account)
        left = max(0.0, round(left, 4))
    except (OSError, ValueError, KeyError, TypeError) as e:
        # e.g. another provider behind OPENROUTER_URL: nothing is known
        log.info("Funds of key %s unknown: %s", _mark(key), shorten(e, 120))
    with _funds_lock:
        known = _funds.setdefault(_mark(key), {"left": None})
        if left is not None or known["left"] is None:
            known["left"] = left
        known.update(at=time.monotonic(), asking=False)
    if left is not None:
        log.info("Funds of key %s: $%.3f", _mark(key), left)


def funds(key, wait=False):
    """Dollars left on the key for paid models, or None when that isn't
    known (yet). Answers from memory; when what it remembers is old, asks
    OpenRouter in the background (or first, with wait)."""
    if not key:
        return None
    with _funds_lock:
        known = _funds.setdefault(_mark(key), {"left": None, "at": 0.0,
                                               "asking": False})
        ask = time.monotonic() - known["at"] > FUNDS_TTL and \
            not known["asking"]
        if ask:
            known["asking"] = True
    if ask and wait:
        _ask_funds(key)
    elif ask:
        threading.Thread(target=_ask_funds, args=(key,), daemon=True).start()
    return _funds[_mark(key)]["left"]


def funds_status(key, wait=False):
    """How a key's funds stand: whether paid models can be used, whether
    the funds are running out, what's left (None: not known)."""
    left = funds(key, wait)
    return {"available": left is None or left > MIN_FUNDS,
            "low": left is not None and left < config.OPENROUTER_LOW_FUNDS,
            "left": left}


def check_key(key):
    """Asks OpenRouter about a key: BadKey if it doesn't know it, LLMError
    if it can't be asked now. A provider that has no such question (another
    one behind OPENROUTER_URL) leaves the key unchecked."""
    try:
        _get("/auth/key", key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadKey("HTTP %d" % e.code)
        if e.code not in (404, 405):
            raise LLMError("HTTP %d" % e.code)
        log.info("The provider can't check keys (HTTP %d)", e.code)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise LLMError("the key can't be checked: %s" % shorten(e, 120))
    with _funds_lock:
        _funds.pop(_mark(key), None)
    return funds_status(key, wait=True)


# --- the models there are ------------------------------------------------

def _price(value):
    try:
        return float(value) * 1e6  # dollars a million tokens
    except (TypeError, ValueError):
        return None


def catalogue():
    """OpenRouter's models that write text: [{id, name, input, output,
    free}], prices in dollars a million tokens; None when the list can't
    be had. Remembered for an hour."""
    with _catalogue_lock:
        fresh = time.monotonic() - _catalogue["at"] < CATALOGUE_TTL
        if fresh and _catalogue["models"] is not None:
            return _catalogue["models"]
        try:
            models = []
            for m in _get("/models"):
                price = m.get("pricing") or {}
                modes = (m.get("architecture") or {}).get("output_modalities")
                if modes and "text" not in modes:
                    continue
                cost_in, cost_out = _price(price.get("prompt")), \
                    _price(price.get("completion"))
                if cost_in is None or cost_out is None or cost_in < 0:
                    continue
                models.append({"id": m["id"], "name": m.get("name") or m["id"],
                               "input": round(cost_in, 4),
                               "output": round(cost_out, 4),
                               "free": cost_in == 0 and cost_out == 0})
            _catalogue.update(models=sorted(models, key=lambda m: m["id"]),
                              at=time.monotonic())
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("No list of models: %s", shorten(e, 120))
            _catalogue["at"] = time.monotonic() - CATALOGUE_TTL + 60
        return _catalogue["models"]


# --- completions ---------------------------------------------------------

def _cached(messages, model):
    """The messages, with the system prompt marked for caching where a
    model caches only on request (Anthropic's): it's the same for every
    CV read, and a cached read costs a tenth. Other providers cache by
    themselves or not at all."""
    if "anthropic/" not in model or not messages or \
            messages[0]["role"] != "system":
        return messages
    first = dict(messages[0], content=[{
        "type": "text", "text": messages[0]["content"],
        "cache_control": {"type": "ephemeral"}}])
    return [first] + messages[1:]


def complete(messages, purpose, model, key, think=True):
    """One chat completion: returns (text of the answer, model name).
    think=False asks a reasoning model not to reason first (OpenRouter's
    "reasoning" setting; others ignore it): for work that needs none, like
    copying a CV into JSON, it's several times faster and cheaper."""
    if not key:
        raise BadKey("no API key")
    # No OpenRouter fallback ("models"): a model that's down or
    # rate-limited fails fast and ask_json() moves on to the next one.
    payload = {"model": model, "messages": _cached(messages, model)}
    if not think:
        payload["reasoning"] = {"enabled": False}
    if config.OPENROUTER_TEMPERATURE:
        payload["temperature"] = float(config.OPENROUTER_TEMPERATURE)
    size = sum(len(m["content"]) for m in messages)
    log.info("Request (%s), model %s, prompt %d chars", purpose, model, size)
    started = time.monotonic()
    result = _post(payload, key)
    elapsed = time.monotonic() - started
    if "choices" not in result or not result["choices"]:
        raise LLMError("no answer: %s" % shorten(json.dumps(
            result.get("error", result), ensure_ascii=False)))
    answered_by = result.get("model", "?")
    usage = result.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    log.info("Response (%s) from %s in %.1fs (tokens: %s in%s, %s out)",
             purpose, answered_by, elapsed, usage.get("prompt_tokens", "?"),
             ", %s of them cached" % cached if cached else "",
             usage.get("completion_tokens", "?"))
    text = (result["choices"][0].get("message") or {}).get("content") or ""
    if not text.strip():
        raise LLMError("%s returned an empty answer" % answered_by)
    return text.strip(), answered_by


def _rest(key, model, seconds=RATE_LIMIT_REST):
    _resting[(_mark(key), model)] = time.monotonic() + seconds
    log.info("Leaving %s out for %ds", model, seconds)


def ladder(account):
    """The models to ask for an account, in order, as (model, tier): its
    paid ones (tier "paid") when it wants them and has funds, its free
    ones ("free"), and at last, as often as OPENROUTER_ATTEMPTS says, the
    one nobody can take off the list (OPENROUTER_MODEL, "last")."""
    last = config.OPENROUTER_MODEL
    rungs = []
    if account.get("use_paid") and funds_status(account["key"])["available"]:
        rungs += [(m, "paid") for m in account["paid"]]
    rungs += [(m, "free") for m in account["free"]]
    seen, unique = {last}, []
    for model, tier in rungs:
        if model not in seen:
            seen.add(model)
            unique.append((model, tier))
    return unique + [(last, "last")] * config.OPENROUTER_ATTEMPTS


def ask_json(messages, purpose, validate, account, think=True):
    """complete(), retried until the answer is a JSON object that
    `validate(parsed)` accepts (it raises LLMError or ValueError on an
    unusable one); returns (what validate returned, the raw answer text,
    who answered: {model, asked, tier}). `think`: see complete().

    The account's ladder() is climbed one attempt a model. A model that's
    rate-limited is asked once more after a pause; one that still is, or
    that didn't answer in time, gets left out of the account's next
    requests for a while, and one that refused (a paid model with no
    credit left, a model that's gone) for longer, so that the models
    after it take over. Raises BadKey at once when the key is no good,
    LLMError when every attempt failed."""
    key = account.get("key")
    if not key:
        raise BadKey("no API key")
    now = time.monotonic()
    rungs = [(m, tier) for m, tier in ladder(account)
             if tier == "last" or _resting.get((_mark(key), m), 0) <= now]
    for attempt, (model, tier) in enumerate(rungs, 1):
        try:
            try:
                text, answered_by = complete(messages, purpose, model, key,
                                             think)
            except RateLimited as e:
                log.info("%s is rate-limited, once more in %ds: %s", model,
                         RATE_LIMIT_PAUSE, shorten(e, 120))
                time.sleep(RATE_LIMIT_PAUSE)
                try:
                    text, answered_by = complete(messages, purpose, model,
                                                 key, think)
                except RateLimited:
                    if tier != "last":
                        _rest(key, model)
                    raise
            try:
                return validate(parse_json_object(text)), text, {
                    "model": answered_by, "asked": model, "tier": tier}
            except (LLMError, ValueError) as e:
                raise LLMError("unusable answer from %s: %s: %s"
                               % (answered_by, e, shorten(text)))
        except BadKey:
            raise
        except LLMError as e:
            if isinstance(e, TimedOut) and tier != "last":
                _rest(key, model)
            elif isinstance(e, Refused) and tier != "last":
                _rest(key, model, REFUSED_REST)
                with _funds_lock:  # perhaps the money has run out
                    _funds.get(_mark(key), {})["at"] = 0.0
            log.warning("Attempt %d/%d (%s) failed: %s",
                        attempt, len(rungs), purpose, e)
            if attempt == len(rungs):
                raise
