"""CV Aggregator: a candidate's profile gathered from all their CVs, and
job offers from the portals that fit it. The app's entry point: prepares the
database (db.py), starts the background workers reading CVs (worker.py)
and looking for offers (offers.py) and serves the web page
(web_server.py).

    python3 app.py                         serve
    python3 app.py --add-user USER         make an account (the password is
                                           asked for, or read from stdin),
                                           also with ALLOW_REGISTRATION off
    python3 app.py --set-password USER     set a user's password (same)
    python3 app.py --set-key USER          give a user an OpenRouter key
                                           (asked for, or read from stdin;
                                           left empty: OPENROUTER_API_KEY
                                           of .env)
"""

import argparse
import getpass
import logging
import os
import sys

import config
import db
import llm
import offers
import portals
import web_server
import worker

log = logging.getLogger("app")


def _ask_password(username):
    if sys.stdin.isatty():
        password = getpass.getpass("Nowe hasło dla %s: " % username)
        if getpass.getpass("Powtórz hasło: ") != password:
            sys.exit("Hasła się różnią.")
        return password
    return sys.stdin.readline().rstrip("\n")


def add_user(username):
    password = _ask_password(username)
    conn = db.connect()
    try:
        user = db.create_user(conn, username, password)
    except db.Error as e:
        sys.exit(str(e))
    finally:
        conn.close()
    print("Utworzono konto %s." % user["username"])


def set_password(username):
    password = _ask_password(username)
    conn = db.connect()
    try:
        stored = db.set_password(conn, username, password)
    except db.Error as e:
        sys.exit(str(e))
    finally:
        conn.close()
    if not stored:
        sys.exit("Nie ma użytkownika %s." % username)
    print("Hasło użytkownika %s zmienione; jego logowania zostały "
          "zakończone." % stored)


def set_key(username):
    if sys.stdin.isatty():
        key = getpass.getpass("Klucz API OpenRoutera dla %s (pusty: klucz "
                              "z .env): " % username)
    else:
        key = sys.stdin.readline()
    key = "".join(key.split()) or config.OPENROUTER_API_KEY
    if not key:
        sys.exit("Nie podano klucza, a w .env nie ma OPENROUTER_API_KEY.")
    conn = db.connect()
    try:
        user = db.find_user(conn, username)
        if not user:
            sys.exit("Nie ma użytkownika %s." % username)
        try:
            llm.check_key(key)
        except llm.BadKey:
            sys.exit("OpenRouter nie rozpoznaje tego klucza.")
        except llm.LLMError as e:
            sys.exit("Nie udało się sprawdzić klucza: %s" % e)
        db.set_api_key(conn, user["id"], key)
    finally:
        conn.close()
    print("Użytkownik %s ma teraz klucz kończący się na …%s."
          % (user["username"], key[-4:]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--add-user", metavar="USER",
                        help="make an account and exit")
    parser.add_argument("--set-password", metavar="USER",
                        help="set a user's password and exit")
    parser.add_argument("--set-key", metavar="USER",
                        help="give a user an OpenRouter API key and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    db.init()
    if args.add_user:
        return add_user(args.add_user)
    if args.set_password:
        return set_password(args.set_password)
    if args.set_key:
        return set_key(args.set_key)
    portals.load()
    worker.start(max(1, config.WORKERS))
    offers.start()
    web_server.serve()


if __name__ == "__main__":
    main()
