"""
Outbound mail. Sends via the same Teletrac mailbox mail_reader.py already
reads from (EMAIL_ADDRESS/EMAIL_PASSWORD) - no new vendor, and replies
land where the app already looks. SMTP submission (port 587, STARTTLS)
is the standard companion to the IMAP connection already in use; if this
mailbox turns out not to support it, swap send() for a transactional
provider without touching any caller.

Sending is deliberately never allowed to break the caller: a feedback
comment is saved to the Sheet first and always succeeds or fails on its
own. Email is a best-effort step after that, and its failure is
returned as a value, not raised, exactly like sheets_store functions
already do for the Sheets API itself.
"""

import os
import smtplib
import uuid
from email.message import EmailMessage

SMTP_HOST = "mail.teletracfleets.com"
SMTP_PORT = 587
# Fallback for networks that block outbound 587 (common on residential/
# office connections that block anti-spam-prone SMTP ports) but allow
# implicit-TLS submission - same mailbox, same credentials, just a
# different port/handshake. Confirmed reachable where 587 timed out.
SMTP_SSL_PORT = 465


def send(to_addrs, subject, html_body, preheader="", in_reply_to=None, references=None):
    """
    Sends one HTML email. Returns (message_id, None) on success, or
    (None, error_string) on failure - never raises, so a mail outage
    can't take down the feedback flow that triggered it.

    preheader is hidden inline (display:none) at the very top of the
    body - Gmail, Gmail mobile, Outlook mobile and Apple Mail all render
    it as the inbox preview snippet, which is what lets the subject stay
    frozen while the client still sees what changed at a glance.
    """
    address = os.environ.get("EMAIL_ADDRESS")
    password = os.environ.get("EMAIL_PASSWORD")
    if not address or not password:
        return None, "EMAIL_ADDRESS / EMAIL_PASSWORD not configured"
    if not to_addrs:
        return None, "no recipients"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = ", ".join(to_addrs)
    # Deliberately NOT setting our own Message-ID header - confirmed by a
    # controlled A/B test (identical message, only this header changed)
    # that a client-generated Message-ID gets this mail silently dropped
    # somewhere between this server and the recipient, no SMTP-level
    # rejection at all (clean 250 OK either way). Leaving it unset lets
    # the relay (an Exim-based host) assign its own, which is what every
    # successful send in that test had in common. Threading across a
    # case's emails already works from the shared subject line alone
    # (confirmed: multiple sends with the same subject land in one Gmail
    # thread) - In-Reply-To/References below are a secondary hint only,
    # not load-bearing, so this doesn't break anything by going missing.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)

    preheader_html = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all;">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>'
        if preheader else ""
    )
    msg.set_content("This message requires an HTML-capable email client to view.")
    msg.add_alternative(preheader_html + html_body, subtype="html")

    # Purely an internal bookkeeping token for sheets_store's thread
    # ledger (record_sent_message) - never attached to the outgoing
    # message itself, see the note above on why.
    local_id = f"<{uuid.uuid4()}@{address.split('@')[-1]}>"

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(address, password)
            smtp.send_message(msg)
        return local_id, None
    except (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected):
        # Port 587 itself was unreachable (blocked/filtered), not a mailbox
        # or content problem - worth one retry over 465 before giving up,
        # since that's a network-level failure, not an authoritative "no".
        pass
    except Exception as e:
        return None, str(e)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_SSL_PORT, timeout=20) as smtp:
            smtp.login(address, password)
            smtp.send_message(msg)
        return local_id, None
    except Exception as e:
        return None, str(e)
