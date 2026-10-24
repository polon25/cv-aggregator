"""The text of an uploaded CV, and what the program can tell from it without
a model: extract() reads a file (PDF with pdftotext, DOCX and ODT from their
XML, plain text; DOC and RTF made into a PDF by LibreOffice when it's
installed), clean() takes out what a model needn't read (page numbers,
headers repeated on every page, the data processing consent), language()
guesses the CV's language and contacts() finds e-mail addresses, phone
numbers and links.
"""

import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from xml.etree import ElementTree

KINDS = {"pdf": "PDF", "docx": "Word", "odt": "OpenDocument", "doc": "Word 97",
         "rtf": "RTF", "txt": "tekst", "md": "tekst"}
# Text shorter than this isn't worth reading: a scan without a text layer.
MIN_TEXT = 150
CONVERT_SECONDS = 90


class Unreadable(ValueError):
    """A file whose text can't be had, with a message for the user."""


def kind_of(data, filename):
    """The file's kind (a key of KINDS) by its contents, or by its name for
    plain text; None for anything else."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
                mimetype = z.read("mimetype").decode("ascii", "replace") \
                    if "mimetype" in names else ""
        except zipfile.BadZipFile:
            return None
        if "word/document.xml" in names:
            return "docx"
        if "opendocument.text" in mimetype and "content.xml" in names:
            return "odt"
        return None
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    if data.startswith(b"{\\rtf"):
        return "rtf"
    if ext in ("txt", "md") and b"\0" not in data[:4096]:
        return ext
    return None


def extract(path, kind):
    """The text of the file at path, cleaned (see clean()). Raises
    Unreadable."""
    if kind == "pdf":
        text = _pdf(path)
    elif kind == "docx":
        text = _docx(path)
    elif kind == "odt":
        text = _odt(path)
    elif kind in ("doc", "rtf"):
        text = _office(path)
    elif kind in ("txt", "md"):
        with open(path, "rb") as f:
            text = _decode(f.read())
    else:
        raise Unreadable("Nieobsługiwany rodzaj pliku.")
    text = clean(text)
    if len(re.sub(r"\W", "", text)) < MIN_TEXT:
        raise Unreadable("W pliku prawie nie ma tekstu – to pewnie skan. "
                         "Wgraj CV zapisane jako tekst (PDF z edytora, DOCX).")
    return text


def _decode(data):
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1")


def _pdf(path):
    """pdftotext's -layout keeps a line's columns together: a CV's dates
    stay beside what they date."""
    if not shutil.which("pdftotext"):
        raise Unreadable("Na serwerze brakuje programu pdftotext "
                         "(pakiet poppler-utils).")
    try:
        done = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", path, "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=CONVERT_SECONDS)
    except subprocess.TimeoutExpired:
        raise Unreadable("Odczytywanie PDF-a trwało za długo.")
    if done.returncode != 0:
        raise Unreadable("Nie udało się odczytać PDF-a (uszkodzony "
                         "albo zabezpieczony hasłem?).")
    return done.stdout.decode("utf-8", "replace")


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Word keeps a copy of a text box for older readers: read it once.
FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _docx(path):
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise Unreadable("Nie udało się odczytać pliku DOCX.")
    parts = []

    def walk(node):
        if node.tag == FALLBACK:
            return
        if node.tag == W + "t":
            parts.append(node.text or "")
        elif node.tag == W + "tab":
            parts.append("\t")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
        for child in node:
            walk(child)
        # a paragraph (also one in a text box) ends a line, a table's
        # cell is set apart from the next
        if node.tag in (W + "p", W + "tr"):
            parts.append("\n")
        elif node.tag == W + "tc":
            parts.append("   ")

    walk(root)
    return "".join(parts)


T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _odt(path):
    try:
        with zipfile.ZipFile(path) as z:
            root = ElementTree.fromstring(z.read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise Unreadable("Nie udało się odczytać pliku ODT.")
    parts = []

    def walk(node):
        if node.tag == T + "tab":
            parts.append("\t")
        elif node.tag == T + "line-break":
            parts.append("\n")
        elif node.tag == T + "s":
            parts.append(" " * int(node.get(T + "c") or 1))
        elif node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if node.tag in (T + "p", T + "h", TABLE + "table-row"):
            parts.append("\n")
        elif node.tag == TABLE + "table-cell":
            parts.append("   ")

    walk(root.find("{urn:oasis:names:tc:opendocument:xmlns:office:1.0}body")
         or root)
    return "".join(parts)


def _office(path):
    """Old Word and RTF files, through LibreOffice made into a PDF, which
    is then read as any other."""
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not office:
        raise Unreadable("Ten rodzaj pliku wymaga LibreOffice na serwerze; "
                         "zapisz CV jako PDF albo DOCX.")
    with tempfile.TemporaryDirectory() as out:
        try:
            subprocess.run(
                [office, "--headless", "-env:UserInstallation=file://" + out,
                 "--convert-to", "pdf", "--outdir", out, path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=CONVERT_SECONDS)
        except subprocess.TimeoutExpired:
            raise Unreadable("Odczytywanie pliku trwało za długo.")
        pdf = os.path.join(out, os.path.splitext(os.path.basename(path))[0]
                           + ".pdf")
        if not os.path.exists(pdf):
            raise Unreadable("Nie udało się odczytać pliku.")
        return _pdf(pdf)


# --- cleaning ------------------------------------------------------------

PAGE_NUMBER = re.compile(
    r"\s*(?:(?:strona|page|str\.)\s*)?\d{1,2}\s*(?:(?:/|z|of|из)\s*\d{1,2})?\s*",
    re.I)
CONSENT = re.compile(
    r"(wyrażam zgodę na przetwarzanie|zgodę na przetwarzanie moich danych|"
    r"\b(agree|consent) to (the )?process|\bprocessing of my personal data|"
    r"\bRODO\b|\bGDPR\b)", re.I)
# Font icons (Font Awesome and the like) live in the private use area.
PRIVATE_USE = re.compile("[-]")


def clean(text):
    """The text without what tells nothing about the candidate: page
    numbers, lines repeated on every page (headers, footers), the clause
    consenting to data processing (from its first line to the end of its
    paragraph); runs of spaces from a layout shortened,
    font icons dropped."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n"))
    text = PRIVATE_USE.sub("", text).replace("­", "")
    pages = [p.split("\n") for p in text.split("\f")]
    pages = [p for p in pages if any(line.strip() for line in p)]
    if len(pages) >= 2:
        seen = {}
        for page in pages:
            for line in {re.sub(r"\s+", " ", l).strip() for l in page}:
                if len(line) >= 5:
                    seen[line] = seen.get(line, 0) + 1
        repeated = {line for line, n in seen.items() if n == len(pages)}
    else:
        repeated = set()
    lines = []
    for page in pages:
        for line in page:
            flat = re.sub(r"\s+", " ", line).strip()
            if flat in repeated or PAGE_NUMBER.fullmatch(line):
                continue
            lines.append(re.sub(r" {4,}", "   ", line.rstrip()))
        lines.append("")
    blocks = []
    for block in re.split(r"\n\s*\n", "\n".join(lines)):
        # the clause runs from its first line to the end of its paragraph
        block = block.split("\n")
        cut = next((n for n, line in enumerate(block)
                    if CONSENT.search(line)), len(block))
        if any(line.strip() for line in block[:cut]):
            blocks.append("\n".join(block[:cut]).strip("\n"))
    return "\n\n".join(blocks).strip()


# --- what the program reads by itself --------------------------------------

POLISH = re.compile(r"\b(i|w|oraz|na|z|do|się|dla|od|jako|języka?|"
                    r"doświadczenie|umiejętności|wykształcenie)\b", re.I)
ENGLISH = re.compile(r"\b(and|the|of|in|for|with|to|as|experience|skills|"
                     r"education)\b", re.I)
GERMAN = re.compile(r"\b(und|der|die|das|mit|für|von|berufserfahrung|"
                    r"kenntnisse|ausbildung)\b", re.I)


def language(text):
    """"pl", "en", "de" or "" (can't tell), by common words and Polish
    letters."""
    scores = {
        "pl": len(POLISH.findall(text)) +
        len(re.findall(r"[ąćęłńśźż]", text, re.I)) / 3,
        "en": len(ENGLISH.findall(text)),
        "de": len(GERMAN.findall(text)) + len(re.findall(r"[äöüß]", text)),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 10 else ""


EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ \-.]?)?(?:\(?\d{2,3}\)?[ \-.]?)"
                   r"\d{3}[ \-.]?\d{2,3}(?:[ \-.]?\d{2,3})?(?![\w])")
URL = re.compile(r"(?:https?://|www\.)?(?:[a-z0-9\-]+\.)+(?:com|pl|org|net|"
                 r"io|dev|eu|me|info|app)(?:/[\w\-./%~?=&#+]*)?", re.I)


def digits(text):
    return re.sub(r"\D", "", text or "")


def contacts(text):
    """{emails, phones, links} written in the text, in order."""
    emails = list(dict.fromkeys(EMAIL.findall(text)))
    without = EMAIL.sub(" ", text)
    phones = []
    for m in PHONE.finditer(without):
        number = " ".join(m.group(0).split())
        # years ("2019 – 2024") and date ranges aren't phone numbers
        if 9 <= len(digits(number)) <= 13 and \
                not re.fullmatch(r"(19|20)\d\d\D+(19|20)\d\d", number):
            phones.append(number)
    links = []
    for m in URL.finditer(without):
        link = m.group(0).rstrip(".,;)/")
        if "/" in link or link.lower().startswith(("http", "www.")):
            links.append(link)
    return {"emails": emails, "phones": list(dict.fromkeys(phones)),
            "links": list(dict.fromkeys(links))}
