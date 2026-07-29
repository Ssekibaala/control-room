"""
Outbound mail, sent via the Resend HTTP API (resend.com).

This used to go over raw SMTP to the Teletrac mailbox, but Render
blocks outbound traffic on every SMTP port (25, 465, 587) on free web
services - confirmed live: local dev could reach the mailbox, the
deployed app timed out on both 587 and 465. There is no SMTP port
combination that gets around that from Render's network; the only way
out is a transport that isn't SMTP. Resend's API is plain HTTPS
(port 443), which is never blocked - trading "our own mailbox" for
"a provider that actually reaches the recipient" is the least-bad
option that also means never touching this file again if this network
changes its port rules yet again.

Sending is deliberately never allowed to break the caller: a feedback
comment is saved to the Sheet first and always succeeds or fails on its
own. Email is a best-effort step after that, and its failure is
returned as a value, not raised, exactly like sheets_store functions
already do for the Sheets API itself.
"""

import os
import resend

# onboarding@resend.dev is Resend's own shared sandbox sender - works
# immediately with no domain setup, which is why it's the default here,
# but it's Resend's brand in the "From" line, not Teletrac's. Once a
# real sending domain (e.g. teletracfleets.com) is verified in the
# Resend dashboard, set RESEND_FROM_EMAIL to an address on it and every
# email starts coming from the real domain with zero code changes.
DEFAULT_FROM = "onboarding@resend.dev"


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
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return None, "RESEND_API_KEY not configured"
    if not to_addrs:
        return None, "no recipients"

    resend.api_key = api_key
    from_addr = os.environ.get("RESEND_FROM_EMAIL", DEFAULT_FROM)

    preheader_html = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all;">{preheader}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>'
        if preheader else ""
    )

    headers = {}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references:
        headers["References"] = " ".join(references)

    params = {
        "from": from_addr,
        "to": list(to_addrs),
        "subject": subject,
        "html": preheader_html + html_body,
    }
    if headers:
        params["headers"] = headers

    try:
        result = resend.Emails.send(params)
        return result["id"], None
    except Exception as e:
        return None, str(e)
