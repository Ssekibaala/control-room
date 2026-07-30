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
    """Union of client-role user accounts AND the Clients registry's
    contact emails, deduplicated - a client contact doesn't need a login
    to be notified, and a client-role account works even before the
    registry has an entry for them. Every vehicle currently defaults to
    the one existing client (see docs/EMAIL_FEEDBACK_DESIGN.md), so
    registry contacts are additive recipients on every notification,
    not routed per-vehicle yet."""
    seen, out = set(), []
    for email in [r["email"] for r in notification_recipients(roles=("client",))]:
        if email.lower() not in seen:
            seen.add(email.lower())
            out.append(email)
    try:
        for c in sheets_store.load_clients():
            for email in c["emails"]:
                if email.lower() not in seen:
                    seen.add(email.lower())
                    out.append(email)
    except Exception:
        pass  # registry unreachable - role-tagged accounts above still get notified
    return out


def _staff_recipients():
    return [r["email"] for r in notification_recipients(roles=("admin", "technician"))]


def on_comment_added(plate, comment, added_by, role, entry_type, requires_followup,
                     respond_urls=None):
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

    # Every prior comment on this vehicle, newest first - shared by both
    # email types below so a client sees the same trail whether they're
    # the one being asked a question or the one being told the outcome.
    recent = []
    try:
        history = sheets_store.load_feedback_cached().get(plate, {}).get("history", [])
        recent = [
            {"comment": h["comment"], "addedBy": h["addedBy"], "requiresFollowup": h["requiresFollowup"],
             "date": h["date"].strftime("%d %b %Y, %H:%M") if h["date"].year > 1 else ""}
            for h in reversed(history[:-1])
        ]
    except Exception:
        pass

    # A technician/admin submitting with requires_followup left True (or
    # unset) is REPORTING something and asking the client to weigh in -
    # that's the two-button question below. But one submitted as False is
    # a technician who already decided this is resolved on their own, no
    # client input needed to close it - that's an OUTCOME, the exact same
    # shape as a client resolving their own case, not a question. Treating
    # both under one "ask" branch meant a technician explicitly closing
    # something themselves still made the client answer a question that
    # had already been answered - real bug, confirmed against a live send.
    if role in ("admin", "technician") and requires_followup is not False:
        to = _client_recipients()
        if not to:
            result["reason"] = "no client recipients on file"
            return result
        no_url = (respond_urls or {}).get("no_followup", "#")
        need_url = (respond_urls or {}).get("needs_attention", "#")
        html, preheader = email_templates.build_update_email(
            plate, comment, added_by, role, timestamp, no_url, need_url, recent,
            reopened=case["reopened"], previous_closed_at=case["previousClosedAt"])
        return _send_and_record(plate, case, to, html, preheader, result)

    if role in ("client", "admin", "technician"):
        to = list({*_staff_recipients(), *_client_recipients()})
        if not to:
            result["reason"] = "no recipients on file"
            return result
        html, preheader = email_templates.build_outcome_email(
            plate, comment, added_by, requires_followup, timestamp, recent,
            reopened=case["reopened"], previous_closed_at=case["previousClosedAt"])
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


def check_reconnections(classification, recovered, base_url=""):
    """
    Called once per import cycle (see run_import.py), after
    classify_fleet() has produced this cycle's fresh status for every
    plate and run_import.py's _day_over_day() has already worked out
    which plates just transitioned Offline->Online (`recovered`) by
    diffing against what was recorded last cycle
    (sheets_store.load_vehicle_status/save_vehicle_status - there is
    otherwise NO memory of a previous cycle anywhere in this app). This
    function doesn't redo that diff - it only decides, for each plate
    already known to have just come back online, whether there's
    anything worth emailing about.

    A vehicle that just came back online, and still has an unanswered
    follow-up request on file, gets asked whether that resolves it - the
    same two-button email a human update gets, not a silent close. The
    vehicle reconnecting is evidence about connectivity, not proof the
    reported problem (which might not even have been about connectivity)
    is actually fixed, so this is deliberately a question a person still
    has to answer, same as every other state change in this app.

    Returns the list of plates a reconnect-check was actually sent for,
    purely for logging/testing.
    """
    prompted = []
    for plate in recovered:
        info = classification.get(plate) or {}
        fb = info.get("feedback")
        if not fb or fb.get("requiresFollowup") is not True:
            continue  # nothing outstanding to check on for this plate
        try:
            if _send_reconnect_check(plate, info.get("days_silent") or 0, fb["comment"], base_url):
                prompted.append(plate)
        except Exception as e:
            print(f"Reconnect-check email failed for {plate}: {e}")
    return prompted


def send_recovery_notice(classification, recovered):
    """
    Informational-only notice for every vehicle that came back online
    this cycle, regardless of whether it also has an outstanding
    follow-up (that overlapping case still separately gets the
    two-button reconnect-check above, asking whether reconnecting
    resolves it - this notice is a plain FYI, no response needed, and
    goes out alongside that email, not instead of it).

    Added because check_reconnections() only emails about a recovery
    that has an unanswered "needs follow-up" comment on file - most
    recoveries don't, and were reconnecting completely silently, with
    only a quiet dashboard entry (Recovered/New) to show it happened.

    Sent once per import cycle whenever `recovered` is non-empty, to
    both staff and client - there's no interval gate here like the
    weekly digests, because this isn't a snapshot of ongoing state to
    re-send periodically, it's a report of THIS cycle's transitions;
    gating it on a schedule would just mean the recoveries that happen
    between sends are never mentioned at all.
    """
    result = {"sent": False, "reason": None, "count": 0}
    if not recovered:
        result["reason"] = "nothing recovered this cycle"
        return result

    to = list({*_staff_recipients(), *_client_recipients()})
    if not to:
        result["reason"] = "no recipients on file"
        return result

    from datetime import datetime
    vehicles = []
    for plate in recovered:
        info = classification.get(plate) or {}
        fb = info.get("feedback")
        vehicles.append({
            "plate": plate,
            "location": info.get("last_location") or "Unknown",
            "comment": fb["comment"] if fb else None,
        })
    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_recovery_notice_email(vehicles, timestamp)
    message_id, err = mailer.send(to, f"Back online: {len(vehicles)} vehicle(s)", html, preheader)
    if err:
        result["reason"] = err
        return result
    result["sent"], result["count"] = True, len(vehicles)
    return result


def _offline_platform_detail(info):
    """
    Names the actual platform(s) that are silent (e.g. "MiX Unity"), not
    just a count - classifier.py's platform_status keys are already the
    real display names ("Teletrac", "MiX Unity", "FT Cloud Camera", see
    fleet_logic/adapters/*.py's source_platform= values), so no mapping
    is needed, just reading them straight through instead of discarding
    them down to a bare "X/Y platforms" number.
    """
    platforms = info.get("platforms") or []
    offline = sorted(p for p, (s, _) in (info.get("platform_status") or {}).items() if s in ("Offline", "No Data"))
    days = info.get("days_silent") or 0
    tracked = f"{len(platforms)} platform{'s' if len(platforms) != 1 else ''} tracked"
    if offline and len(offline) < len(platforms):
        return f"Offline {days} day(s) on {', '.join(offline)} ({tracked})"
    if offline:
        return f"Offline {days} day(s) on all tracked platforms ({', '.join(offline)})"
    return f"Offline {days} day(s)"


def send_pending_confirmation_digest(classification, base_url, interval_days, overdue_days):
    """
    Weekly (configurable, see settings.ini [digests]) rollup to the
    client of EVERY vehicle currently in Pending Customer Confirmation,
    PLUS every vehicle already in Technical Escalation that has never
    had a single feedback entry - a snapshot of current state, not a
    per-vehicle transition alert like check_reconnections above. A
    vehicle that's been sitting here for a month reappears in every
    digest until it actually resolves; that's intentional, not a bug to
    dedupe away.

    The no-feedback-yet section exists because nothing else in this
    codebase proactively emails the client about a Technical Escalation
    vehicle - on_comment_added() only fires once someone has already
    written a comment, and check_reconnections only fires on an
    Offline->Online transition. A vehicle that escalates and that
    nobody ever comments on would otherwise get zero client
    communication, indefinitely.

    Gated purely on elapsed time since the last send
    (sheets_store.get_digest_last_sent/set_digest_last_sent) - escalation
    to Technical Escalation itself is untouched by any of this, it stays
    exactly the days-based rule in classifier.py it always was. This is
    an additive notification layer, not a gate.
    """
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0}
    try:
        last_sent = sheets_store.get_digest_last_sent("pending_confirmation")
        if last_sent and (datetime.now() - last_sent).days < interval_days:
            result["reason"] = "interval not elapsed"
            return result
    except Exception as e:
        result["reason"] = f"could not check last-sent, skipping to avoid spamming: {e}"
        return result

    pending = sorted(
        (plate, info) for plate, info in classification.items()
        if info.get("status") == "Pending Customer Confirmation"
    )
    critical_no_feedback = sorted(
        (plate, info) for plate, info in classification.items()
        if info.get("status") == "Technical Escalation" and info.get("feedback") is None
    )
    if not pending and not critical_no_feedback:
        result["reason"] = "nothing pending and no unreported critical vehicles"
        return result

    to = _client_recipients()
    if not to:
        result["reason"] = "no client recipients on file"
        return result

    import respond_tokens

    def _vehicle_dict(plate, info, overdue=None):
        v = {
            "plate": plate, "detail": _offline_platform_detail(info),
            "no_url": f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'no_followup')}",
            "need_url": f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'needs_attention')}",
        }
        if overdue is not None:
            v["overdue"] = overdue
        return v

    vehicles = [_vehicle_dict(p, i, overdue=(i.get("days_silent") or 0) >= overdue_days) for p, i in pending]
    critical_vehicles = [_vehicle_dict(p, i) for p, i in critical_no_feedback]

    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_pending_confirmation_digest_email(
        vehicles, timestamp, critical_no_feedback=critical_vehicles)
    total = len(vehicles) + len(critical_vehicles)
    message_id, err = mailer.send(to, f"Weekly check-in: {total} vehicle(s) need your input", html, preheader)
    if err:
        result["reason"] = err
        return result

    try:
        sheets_store.set_digest_last_sent("pending_confirmation")
    except Exception as e:
        print(f"Pending-confirmation digest sent but could not record last-sent timestamp: {e}")
    result["sent"], result["count"] = True, total
    return result


def send_known_issues_checkin_digest(classification, base_url, interval_days):
    """
    Client-facing counterpart to send_pending_confirmation_digest: a
    weekly re-confirmation of every vehicle currently marked Known
    Issue (classifier.py's known_issue override - an earlier explicit
    "no follow-up needed"). That's a point-in-time judgement, not a
    permanent fact - the same problem could recur, or the client's
    circumstances could have changed - so this keeps asking rather than
    treating one old answer as settled forever. Same snapshot-not-
    transition shape and interval-gating as the other two digests.
    """
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0}
    try:
        last_sent = sheets_store.get_digest_last_sent("known_issues_checkin")
        if last_sent and (datetime.now() - last_sent).days < interval_days:
            result["reason"] = "interval not elapsed"
            return result
    except Exception as e:
        result["reason"] = f"could not check last-sent, skipping to avoid spamming: {e}"
        return result

    known = sorted(
        (plate, info) for plate, info in classification.items()
        if info.get("status") == "Known Issue"
    )
    if not known:
        result["reason"] = "no known issues on file"
        return result

    to = _client_recipients()
    if not to:
        result["reason"] = "no client recipients on file"
        return result

    import respond_tokens
    vehicles = []
    for plate, info in known:
        fb = info.get("feedback") or {}
        date = fb.get("date")
        vehicles.append({
            "plate": plate,
            "last_comment": fb.get("comment") or "No follow-up needed.",
            "last_author": fb.get("addedBy") or "unknown",
            "last_date": date.strftime("%d %b %Y") if hasattr(date, "strftime") else "",
            "no_url": f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'no_followup')}",
            "need_url": f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'needs_attention')}",
        })

    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_known_issues_checkin_digest_email(vehicles, timestamp)
    message_id, err = mailer.send(to, f"Quick check-in: please reconfirm {len(vehicles)} known issue(s)", html, preheader)
    if err:
        result["reason"] = err
        return result

    try:
        sheets_store.set_digest_last_sent("known_issues_checkin")
    except Exception as e:
        print(f"Known-issues check-in digest sent but could not record last-sent timestamp: {e}")
    result["sent"], result["count"] = True, len(vehicles)
    return result


def send_technical_escalation_digest(classification, base_url, interval_days):
    """Staff-facing counterpart to send_pending_confirmation_digest -
    same interval-gated snapshot mechanism, different audience and no
    respond-token links (staff act from the dashboard directly, not by
    clicking an email button - see the "Open in dashboard" deep link,
    handled by templates/dashboard.html's boot())."""
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0}
    try:
        last_sent = sheets_store.get_digest_last_sent("technical_escalation")
        if last_sent and (datetime.now() - last_sent).days < interval_days:
            result["reason"] = "interval not elapsed"
            return result
    except Exception as e:
        result["reason"] = f"could not check last-sent, skipping to avoid spamming: {e}"
        return result

    escalated = sorted(
        (plate, info) for plate, info in classification.items()
        if info.get("status") == "Technical Escalation"
    )
    if not escalated:
        result["reason"] = "nothing escalated"
        return result

    to = _staff_recipients()
    if not to:
        result["reason"] = "no staff recipients on file"
        return result

    vehicles = [
        {
            "plate": plate, "detail": _offline_platform_detail(info),
            "severity": info.get("severity") or "Escalated",
            "dashboard_url": f"{base_url}/?plate={plate}" if base_url else "",
        }
        for plate, info in escalated
    ]

    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_technical_escalation_digest_email(vehicles, timestamp)
    message_id, err = mailer.send(
        to, f"Weekly check-in: {len(vehicles)} vehicle(s) in Technical Escalation", html, preheader)
    if err:
        result["reason"] = err
        return result

    try:
        sheets_store.set_digest_last_sent("technical_escalation")
    except Exception as e:
        print(f"Escalation digest sent but could not record last-sent timestamp: {e}")
    result["sent"], result["count"] = True, len(vehicles)
    return result


def send_tamper_risk_report_digest(tampering, base_url, interval_days):
    """
    Weekly client-facing summary of the tampering/location-integrity
    analysis (see importer/tamper_engine.py and email_templates.build_
    tamper_risk_report_email's docstring for the confirmed/unconfirmed
    distinction). Unlike the other three digests, this isn't triggered
    by the dashboard's manual "check-in" button - it runs purely on its
    own weekly schedule, since it isn't the kind of thing a "check in
    on outstanding items right now" action should also mean.

    `tampering` is process_reports()'s dict (confirmed/unconfirmed
    already filtered against tamper checks, summary, severity_bands,
    top_vehicles) plus a checked_assets list threaded in by the caller.
    Gated the same way as the other digests: nothing to report -> no
    send, no state write, silently skipped rather than mailing an
    empty report every week.
    """
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0}
    try:
        last_sent = sheets_store.get_digest_last_sent("tamper_risk_report")
        if last_sent and (datetime.now() - last_sent).days < interval_days:
            result["reason"] = "interval not elapsed"
            return result
    except Exception as e:
        result["reason"] = f"could not check last-sent, skipping to avoid spamming: {e}"
        return result

    summary = tampering.get("summary", {})
    if not summary.get("confirmed") and not summary.get("unconfirmed"):
        result["reason"] = "nothing confirmed or unconfirmed this cycle"
        return result

    to = _client_recipients()
    if not to:
        result["reason"] = "no client recipients on file"
        return result

    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_tamper_risk_report_email(
        summary, tampering.get("severity_bands", []), tampering.get("top_vehicles", []),
        tampering.get("checked_assets", []), timestamp)
    message_id, err = mailer.send(
        to, f"Tampering report: {summary.get('confirmed', 0)} confirmed, {summary.get('unconfirmed', 0)} unconfirmed",
        html, preheader)
    if err:
        result["reason"] = err
        return result

    try:
        sheets_store.set_digest_last_sent("tamper_risk_report")
    except Exception as e:
        print(f"Tamper risk report sent but could not record last-sent timestamp: {e}")
    result["sent"], result["count"] = True, len(tampering.get("top_vehicles", []))
    return result


def _send_reconnect_check(plate, days_offline, open_comment, base_url):
    from datetime import datetime
    import respond_tokens

    if not base_url:
        # Respond links with no domain are dead links - worse than not
        # sending at all, since a client clicking one lands nowhere.
        print(f"Reconnect-check for {plate} skipped: no base URL configured "
              f"(set PUBLIC_BASE_URL, or run on Render where it's automatic).")
        return False

    to = _client_recipients()
    if not to:
        return False

    case = sheets_store.get_or_create_open_case(plate)
    recent = []
    try:
        history = sheets_store.load_feedback_cached().get(plate, {}).get("history", [])
        # Exclude the last entry - that's the same open ask already shown
        # above in its own highlighted box, showing it twice would be
        # redundant rather than informative.
        recent = [
            {"comment": h["comment"], "addedBy": h["addedBy"], "requiresFollowup": h["requiresFollowup"],
             "date": h["date"].strftime("%d %b %Y, %H:%M") if h["date"].year > 1 else ""}
            for h in reversed(history[:-1])
        ]
    except Exception:
        pass

    no_url = f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'no_followup')}"
    need_url = f"{base_url}/feedback/respond?token={respond_tokens.make_respond_token(plate, 'needs_attention')}"
    timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_reconnect_check_email(
        plate, days_offline, open_comment, timestamp, no_url, need_url, recent)

    result = {"sent": False, "reason": None, "case_id": case["caseId"]}
    sent = _send_and_record(plate, case, to, html, preheader, result)
    return sent["sent"]


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
