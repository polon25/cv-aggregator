"""The mailbox job alerts are forwarded to (pracuj.pl's, LinkedIn's: the app
can't read those portals itself), one per user, over IMAP: check() logs
in and says how many messages there are, collect() reads the mails that
came since the last time; each with links to offers (by the "link"
patterns of portals/, whoever sent it: forwarded by hand too) becomes a
page of tiles for importer.py, as a results page sent from the browser.
Mails are read from the folder opened read-only (EXAMINE), with
BODY.PEEK, so reading changes nothing, not even marks them read; trash()
then moves the alerts whose offers the app kept to the mailbox's trash
(unless the user turned that off), where the provider deletes them later
(Gmail after 30 days), to keep the mailbox from filling up.

The user's own mailbox is meant to forward the alerts to one kept for the
app alone (e.g. a Gmail account with an app password), so that the server
holds no password to their private mail.
"""

import datetime
import email
import email.errors
import email.header
import email.policy
import html
import imaplib
import re
import socket
import ssl
from html.parser import HTMLParser

import config
import portals

TIMEOUT = 20
TILE_CHARS = 600
# The IMAP server of common mail providers, by the address' domain.
HOSTS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
         "wp.pl": "imap.wp.pl", "o2.pl": "poczta.o2.pl",
         "onet.pl": "imap.poczta.onet.pl", "op.pl": "imap.poczta.onet.pl",
         "interia.pl": "poczta.interia.pl", "poczta.fm": "poczta.interia.pl",
         "gazeta.pl": "imap.gazeta.pl", "icloud.com": "imap.mail.me.com",
         "yahoo.com": "imap.mail.yahoo.com"}
HOST_RE = re.compile(r"[A-Za-z0-9.\-]{3,253}")


class AlertsError(Exception):
    """A mailbox that can't be used, with a message for the user."""


class _IMAP(imaplib.IMAP4_SSL):
    """IMAP over TLS with a timeout (Python 3.8's imaplib has none)."""

    def _create_socket(self, *args):
        sock = socket.create_connection((self.host, self.port), TIMEOUT)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


def host_for(address):
    """The IMAP server of an address' provider, "" if not known."""
    return HOSTS.get(address.rpartition("@")[2].lower(), "")


def clean(settings):
    """{address, host, port, folder} from the settings form; raises
    AlertsError."""
    address = str(settings.get("address") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) or \
            len(address) > 200:
        raise AlertsError("Podaj adres skrzynki, np. moje.alerty@gmail.com.")
    host = str(settings.get("host") or "").strip() or host_for(address)
    if not HOST_RE.fullmatch(host):
        raise AlertsError("Podaj serwer IMAP skrzynki (np. imap.gmail.com).")
    try:
        port = int(settings.get("port") or 993)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        raise AlertsError("Nieprawidłowy port.")
    folder = str(settings.get("folder") or "").strip() or "INBOX"
    return {"address": address, "host": host, "port": port,
            "folder": folder[:100]}


def connect(mailbox, password, readonly=True):
    """A logged-in IMAP connection with the folder open (read-only unless
    told otherwise), and the number of messages in it."""
    try:
        imap = _IMAP(mailbox["host"], mailbox["port"],
                     ssl_context=ssl.create_default_context())
    except (OSError, ssl.SSLError) as e:
        raise AlertsError("Nie udało się połączyć z %s:%d (%s)." % (
            mailbox["host"], mailbox["port"], e.__class__.__name__))
    try:
        imap.login(mailbox["address"], password)
    except imaplib.IMAP4.error:
        imap.shutdown()
        raise AlertsError("Serwer nie przyjął adresu albo hasła. W Gmailu "
                          "potrzebne jest hasło do aplikacji, nie hasło do "
                          "konta.")
    status, data = imap.select('"%s"' % mailbox["folder"].replace('"', ""),
                               readonly=readonly)
    if status != "OK":
        imap.logout()
        raise AlertsError("Nie ma folderu „%s”." % mailbox["folder"])
    return imap, int(data[0] or 0)


def check(mailbox, password):
    """Logs in and counts the folder's messages; raises AlertsError."""
    imap, count = connect(mailbox, password)
    try:
        imap.logout()
    except (imaplib.IMAP4.error, OSError):
        pass
    return count


# --- reading the alerts ------------------------------------------------------

VOID = {"br", "img", "meta", "link", "hr", "input", "wbr", "col", "area",
        "base", "source"}
BLOCKS = {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4",
          "table"}


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.kids = []


class _Tree(HTMLParser):
    """An HTML document as a tree of _Nodes and strings, forgiving of
    unclosed tags (as mails' HTML is)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.at = _Node("root", [], None)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.at)
        self.at.kids.append(node)
        if tag not in VOID:
            self.at = node

    def handle_endtag(self, tag):
        node = self.at
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.at = node.parent

    def handle_data(self, data):
        self.at.kids.append(data)


def _text(node):
    parts = []

    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif x.tag not in ("style", "script", "head", "title"):
            if x.tag in BLOCKS:
                parts.append("\n")
            for kid in x.kids:
                walk(kid)
    walk(node)
    text = re.sub(r"[ \t ​‌͏­]+", " ", "".join(parts))
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _links(node):
    if isinstance(node, str):
        return []
    found = [node] if node.tag == "a" and node.attrs.get("href") else []
    for kid in node.kids:
        found += _links(kid)
    return found


def _patterns():
    """The patterns of links to offers of portals whose offers come by
    mail or from the browser (portals.imported())."""
    return [re.compile(portals.PORTALS[p]["import"]["link"])
            for p in portals.imported()
            if portals.PORTALS[p]["import"].get("link")]


def _key(href, patterns):
    for pattern in patterns:
        m = pattern.search(href)
        if m:
            return next((g for g in m.groups() if g), href.split("?")[0])
    return None


def tiles(html_text, plain_text=""):
    """[{url, text}]: each link to an offer in a mail and its tile, the
    largest element around it with no other offer (as the bookmarklet
    finds them, web/importer.js); from the plain text's blocks when the
    mail has no HTML."""
    patterns = _patterns()
    found, seen = [], set()
    if html_text:
        tree = _Tree()
        tree.feed(html_text)
        for link in _links(tree.root):
            href = html.unescape(link.attrs["href"])
            key = _key(href, patterns)
            if not key or key in seen:
                continue
            seen.add(key)
            node = link
            while node.parent and node.parent is not tree.root and not any(
                    _key(html.unescape(b.attrs["href"]), patterns)
                    not in (None, key) for b in _links(node.parent)):
                node = node.parent
            found.append({"url": href, "text": _text(node)[:TILE_CHARS]})
        return found
    for block in re.split(r"\n\s*(?:-{5,}|\n)\s*", plain_text or ""):
        for href in re.findall(r"https?://\S+", block):
            key = _key(href, patterns)
            if key and key not in seen:
                seen.add(key)
                text = re.sub(r"\S*https?://\S+", "", block).strip()
                found.append({"url": href, "text": text[:TILE_CHARS]})
    return found


def _decode(value):
    try:
        return str(email.header.make_header(email.header.decode_header(
            value or "")))
    except (UnicodeError, LookupError, email.errors.HeaderParseError):
        return str(value or "")


def page(raw):
    """A mail as a page for importer.process: its offers' tiles; None if
    it has no links to offers (not an alert)."""
    mail = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        part = mail.get_body(("html",))
        html_text = part.get_content() if part else ""
        part = mail.get_body(("plain",))
        plain_text = part.get_content() if part else ""
    except (LookupError, UnicodeError, KeyError):
        return None
    cards = tiles(html_text, plain_text)
    if not cards:
        return None
    return {"url": "mail:%s" % (mail["Message-ID"] or "").strip("<> ")[:200],
            "title": _decode(mail["Subject"])[:300],
            "sender": _decode(mail["From"])[:200],
            "text": plain_text[:4000], "ld": [], "cards": cards,
            "mail": True}


def collect(mailbox, password):
    """The alerts that came since the mailbox was last read (or, the first
    time, in the last config.MAIL_DAYS days), at most config.MAIL_BATCH:
    ([page], the mailbox's new state). Mails are fetched with BODY.PEEK
    from a folder opened read-only: nothing is marked as read."""
    imap, count = connect(mailbox, password)
    state = dict(mailbox)
    try:
        validity = (imap.response("UIDVALIDITY")[1] or [b""])[0]
        validity = validity.decode() if isinstance(validity, bytes) else ""
        last = mailbox.get("last_uid") if mailbox.get("uidvalidity") == \
            validity else None
        if last:
            status, data = imap.uid("SEARCH", None, "UID %d:*" % (last + 1))
        else:
            since = datetime.date.today() - datetime.timedelta(
                days=config.MAIL_DAYS)
            status, data = imap.uid("SEARCH", None, "SINCE",
                                    since.strftime("%d-%b-%Y"))
        uids = sorted(int(u) for u in (data[0] or b"").split()
                      if int(u) > (last or 0))[:config.MAIL_BATCH]
        found = []
        for uid in uids:
            status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((part[1] for part in data or []
                        if isinstance(part, tuple)), None)
            one = page(raw) if raw else None
            if one:
                one.update(uid=uid, uidvalidity=validity)
                found.append(one)
        state.update(uidvalidity=validity, messages=count,
                     last_uid=max(uids) if uids else last or 0)
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return found, state


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')


def _trash_folder(imap):
    """The mailbox's trash, by its special-use flag (RFC 6154; Gmail's is
    "[Gmail]/Trash", or "[Gmail]/Kosz" in Polish), or None."""
    status, folders = imap.list()
    for line in folders or []:
        m = LIST_RE.match(line.decode("utf-8", "replace") if isinstance(
            line, bytes) else str(line))
        if m and "\\trash" in m.group("flags").lower():
            return m.group("name")
    return None


def trash(mailbox, password, uids):
    """Moves the mails (by UID) to the mailbox's trash; where it has none,
    deletes them. Returns how many were moved or deleted."""
    if not uids:
        return 0
    imap, _ = connect(mailbox, password, readonly=False)
    try:
        found = imap.uid("SEARCH", None, "UID", ",".join(map(str, uids)))[1]
        present = [u for u in (found[0] or b"").split()
                   if int(u) in set(uids)]
        if not present:
            return 0
        which = b",".join(present).decode()
        folder = _trash_folder(imap)
        if folder and folder.strip('"') != mailbox["folder"]:
            if "MOVE" in imap.capabilities:
                status, _ = imap.uid("MOVE", which, folder)
                if status == "OK":
                    return len(present)
            imap.uid("COPY", which, folder)
        imap.uid("STORE", which, "+FLAGS.SILENT", "(\\Deleted)")
        if "UIDPLUS" in imap.capabilities:
            imap.uid("EXPUNGE", which)
        else:
            imap.expunge()
        return len(present)
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
