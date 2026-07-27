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
from email.message import EmailMessage
from email.utils import make_msgid

SMTP_HOST = "mail.teletracfleets.com"
SMTP_PORT = 587


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
    msg["Message-ID"] = make_msgid(domain=address.split("@")[-1])
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

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(address, password)
            smtp.send_message(msg)
        return msg["Message-ID"], None
    except Exception as e:
        return None, str(e)
