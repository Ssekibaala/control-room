"""
Orchestrates the email side of a feedback entry: which thread it belongs
to, who gets told, and what the email says. Called from exactly two
places - the in-app POST /api/feedback route and the email-link confirm
route - so there is one implementation of "what happens when a comment
is added", not two that can drift apart.

Sending is always best-effort. The comment itself is already saved by
the time any of this runs; a mail failure here is logged and returned
as a value, never allowed to turn a successful save into an error the
user sees.
"""

import sheets_store
import mailer
import email_templates
from users import notification_recipients


def _client_recipients():
    return [r["email"] for r in notification_recipients(roles=("client",))]


def _staff_recipients():
    return [r["email"] for r in notification_recipients(roles=("admin", "technician"))]


def on_comment_added(plate, comment, added_by, role, entry_type, requires_followup, respond_urls=None):
    """
    role/entry_type/requires_followup are exactly what was just written
    to the Feedback tab by sheets_store.add_feedback(). respond_urls, if
    given, is {"no_followup": url, "needs_attention": url} - supplied by
    the caller because building them needs a signed token, which is the
    caller's job (app.py), not this module's.

    Returns a small dict of what happened, purely for logging/testing -
    callers are not expected to act on it.
    """
    result = {"sent": False, "reason": None, "case_id": None}
    try:
        case = sheets_store.get_or_create_open_case(plate)
        result["case_id"] = case["caseId"]
    except Exception as e:
        result["reason"] = f"could not open/read thread ledger: {e}"
        return result

    from datetime import datetime
    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")

    if role in ("admin", "technician"):
        to = _client_recipients()
        if not to:
            result["reason"] = "no client recipients on file"
            return result
        recent = []
        try:
            history = sheets_store.load_feedback_cached().get(plate, {}).get("history", [])
            recent = [
                {"comment": h["comment"], "addedBy": h["addedBy"],
                 "date": h["date"].strftime("%d %b %Y, %H:%M") if h["date"].year > 1 else ""}
                for h in reversed(history[:-1])
            ]
        except Exception:
            pass
        no_url = (respond_urls or {}).get("no_followup", "#")
        need_url = (respond_urls or {}).get("needs_attention", "#")
        html, preheader = email_templates.build_update_email(
            plate, comment, added_by, role, timestamp, no_url, need_url, recent)
        return _send_and_record(plate, case, to, html, preheader, result)

    if role == "client":
        to = list({*_staff_recipients(), *_client_recipients()})
        if not to:
            result["reason"] = "no recipients on file"
            return result
        html, preheader = email_templates.build_outcome_email(
            plate, comment, added_by, requires_followup, timestamp)
        sent = _send_and_record(plate, case, to, html, preheader, result)
        if requires_followup is False:
            try:
                sheets_store.close_case(plate, case["caseId"])
            except Exception:
                pass  # the email already went out; a failed status flip just means the NEXT
                      # comment reopens this same case instead of starting a new one - harmless
        return sent

    result["reason"] = f"no notification rule for role {role!r}"
    return result


def _send_and_record(plate, case, to_addrs, html, preheader, result):
    message_id, err = mailer.send(
        to_addrs, case["subject"], html, preheader,
        in_reply_to=case["rootMessageId"] or None,
        references=case["references"] or None,
    )
    if err:
        result["reason"] = err
        return result
    try:
        sheets_store.record_sent_message(plate, case, message_id)
    except Exception:
        pass  # the email already sent; losing this update just means the NEXT email
              # in the case starts a fresh References chain instead of extending it
    result["sent"] = True
    return result
