"""
Mail reader for mail.teletracfleets.com. This is a standard IMAPS
mailbox (cPanel-hosted), NOT Gmail, so Google Apps Script's GmailApp
class cannot read it, GmailApp only works against Gmail/Workspace
mailboxes. Apps Script can still be used as a free scheduler (a
time-driven trigger calling our /api/import endpoint), but the actual
mail reading has to happen here, in Python, via standard IMAP.

Two report shapes arrive by email:
  1. CSV/zip attached directly (Teletrac offline report, FT Cloud camera)
  2. A "your report is ready" notification with a signed download link
     inside the HTML body (all three MiX Unity reports: Movement,
     Current Mobile Status, Power Disconnections)

For shape 2, the naive "grab the first href" approach is a real risk:
these emails contain MULTIPLE links (a company logo link, an
unsubscribe-style footer link, and the actual signed download link).
Which one appears first in the HTML is not guaranteed to stay stable
if Powerfleet ever changes their template. This extracts the download
link by what it IS (a report-dl.za.mixtelematics.com URL, or an
anchor carrying a filename= attribute), not by its position in the
document.
"""

import imaplib
import email
import re
import zipfile
import io
from email.header import decode_header

IMAP_HOST = "mail.teletracfleets.com"
IMAP_PORT = 993

# Known report subjects, matched by substring (case-insensitive)
REPORT_SUBJECTS = {
    "teletrac_offline": "Bulk - Teletrac platform offline report",
    "mix_movement": "Bulk Daily Movement Report GTL",
    "mix_mobile_status": "Bulk Current Mobile Status Report GTL",
    "mix_power_events": "Bulk Event Report Power disconnections GTL",
    "ft_cloud_camera": "Camera status Report All fleet",
}

# A mailbox rule files the three MiX Unity report notifications into a
# subfolder instead of leaving them in INBOX; Teletrac and FT Cloud stay
# in INBOX. Confirmed against the real mailbox, not a guess: searching
# INBOX alone finds 0 of the 3 MiX reports even though they arrive daily.
REPORT_FOLDERS = {
    "teletrac_offline": "INBOX",
    "mix_movement": "INBOX.Mix subscriptions",
    "mix_mobile_status": "INBOX.Mix subscriptions",
    "mix_power_events": "INBOX.Mix subscriptions",
    "ft_cloud_camera": "INBOX",
}

# The domain MiX/Powerfleet always uses for the actual signed CSV download,
# confirmed from real report emails. This is the primary filter.
DOWNLOAD_LINK_DOMAIN = "report-dl"


def connect(username, password, host=IMAP_HOST, port=IMAP_PORT):
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(username, password)
    return conn


def _decode(value):
    if value is None:
        return ""
    parts = decode_header(value)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def find_latest_message(conn, subject_substring, mailbox="INBOX"):
    """Returns the raw email.message.Message for the most recent email
    whose subject contains subject_substring, or None if not found.

    Raises RuntimeError if the mailbox itself can't be opened - this
    used to go unchecked, so a folder that doesn't exist (or an IMAP
    server that renamed/renumbered it) left the connection sitting in
    AUTH state, and the SEARCH call two lines down then failed with a
    raw, confusing "command SEARCH illegal in state AUTH" - which looks
    nothing like "wrong folder name" unless you already know IMAP well
    enough to guess it. Confirmed live: this is exactly what silently
    broke every MiX report (all three live in "INBOX.Mix subscriptions",
    not plain INBOX) once fetch_reports() was fixed to actually reach
    them instead of dying earlier on an unrelated Teletrac check.
    """
    # Quoted unconditionally: IMAP mailbox names containing a space (like
    # "INBOX.Mix subscriptions") are rejected as an invalid atom otherwise,
    # and a quoted plain "INBOX" is still valid per RFC 3501.
    typ, data = conn.select(f'"{mailbox}"')
    if typ != "OK":
        detail = data[0].decode(errors="replace") if data and data[0] else "no further detail from the server"
        raise RuntimeError(f"Could not open mailbox '{mailbox}': {detail}")
    # IMAP SEARCH with quoted substring, case-insensitive per RFC 3501 SUBJECT search
    status, data = conn.search(None, f'(SUBJECT "{subject_substring}")')
    if status != "OK" or not data or not data[0]:
        return None
    ids = data[0].split()
    latest_id = ids[-1]  # message IDs are returned in ascending order
    status, msg_data = conn.fetch(latest_id, "(RFC822)")
    if status != "OK":
        return None
    return email.message_from_bytes(msg_data[0][1])


def extract_attachment(msg, filename_contains=None):
    """
    Returns (filename, bytes) for the first attachment matching
    filename_contains (case-insensitive substring), or the first
    attachment found if filename_contains is None.
    """
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" not in disposition.lower():
            continue
        filename = _decode(part.get_filename())
        if not filename:
            continue
        if filename_contains and filename_contains.lower() not in filename.lower():
            continue
        return filename, part.get_payload(decode=True)
    return None, None


def _get_html_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        return ""
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return ""


def extract_download_link(html_content):
    """
    Finds the real signed-download link inside a MiX Unity notification
    email, robust to link ORDER in the document. Preference order:
      1. Any href whose URL contains the known download domain.
      2. Any <a> tag that also carries a filename= attribute (the
         download button's defining feature, independent of domain).
      3. Fall back to the first href found (last resort, logged as such).
    Returns None if no href exists at all.
    """
    if not html_content:
        return None

    all_anchors = re.findall(r'<a\s+([^>]*)>', html_content, re.IGNORECASE)
    hrefs_with_attrs = []
    for attrs in all_anchors:
        href_match = re.search(r'href\s*=\s*["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        if href_match:
            hrefs_with_attrs.append((href_match.group(1), attrs))

    if not hrefs_with_attrs:
        return None

    # Preference 1: known download domain
    for href, attrs in hrefs_with_attrs:
        if DOWNLOAD_LINK_DOMAIN in href.lower():
            return href

    # Preference 2: anchor carries filename= attribute
    for href, attrs in hrefs_with_attrs:
        if "filename=" in attrs.lower():
            return href

    # Last resort: first href in the document (not reliable, but better than nothing)
    return hrefs_with_attrs[0][0]


def extract_download_link_naive(html_content):
    """The literal 'first href wins' approach. Kept only so the test
    suite can demonstrate why this alone isn't safe to ship."""
    if not html_content:
        return None
    match = re.search(r'href\s*=\s*["\']([^"\']+)["\']', html_content, re.IGNORECASE)
    return match.group(1) if match else None


def extract_zip_csv(zip_bytes):
    """FT Cloud camera report arrives as a zip containing one xlsx.
    Returns the raw xlsx bytes."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        xlsx_name = next(n for n in z.namelist() if n.endswith(".xlsx"))
        return z.read(xlsx_name)
