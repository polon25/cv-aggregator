"""The mailbox job alerts are forwarded to (pracuj.pl's, LinkedIn's: the app
can't read those portals itself), one per user, over IMAP: check() logs
in and says how many messages there are. The mailbox is only ever opened
read-only (EXAMINE), so nothing in it is changed.

The user's own mailbox is meant to forward the alerts to one kept for the
app alone (e.g. a Gmail account with an app password), so that the server
holds no password to their private mail.
"""

import imaplib
import re
import socket
import ssl

TIMEOUT = 20
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


def connect(mailbox, password):
    """A logged-in IMAP connection with the folder open read-only, and the
    number of messages in it."""
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
                               readonly=True)
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
