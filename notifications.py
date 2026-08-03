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

import os
import json

import sheets_store
import mailer
import email_templates
from users import notification_recipients


def _client_recipients(client=None):
    """
    Who to email about ONE client's vehicles: that client's registry
    contacts, plus client-role accounts assigned to them.

    `client` is effectively mandatory for anything vehicle-related.
    This used to return every client contact in the system for every
    notification, which was correct only while there was exactly one
    client - the moment a second existed, every digest mailed one
    client's registration numbers, locations and fault history to
    another company's external contacts. That is the single worst
    failure mode this app has, because unlike a UI leak it cannot be
    taken back once sent.

    client=None keeps the old union and is reserved for the cases where
    it is genuinely correct: nothing vehicle-scoped, only operational
    mail that isn't about a particular fleet. Callers must pass a client
    for anything listing vehicles.
    """
    seen, out = set(), []

    def add(email):
        if email and email.lower() not in seen:
            seen.add(email.lower())
            out.append(email)

    for r in notification_recipients(roles=("client",), client=client):
        add(r["email"])
    try:
        for c in sheets_store.load_clients():
            if client is not None and c["name"].strip() != client:
                continue
            for email in c["emails"]:
                add(email)
    except Exception:
        pass  # registry unreachable - role-tagged accounts above still get notified
    return out


def _clients_of(classification):
    """
    Splits a classification into {client: {plate: info}} so each digest
    can be built and addressed per client.

    A vehicle with no client is grouped under None and skipped by the
    client-facing digests: an unmapped platform account is a
    configuration gap, and there is no defensible way to choose which
    client should receive mail about it. Staff-facing mail still covers
    it, so it isn't silently lost - see send_technical_escalation_digest.
    """
    grouped = {}
    for plate, info in classification.items():
        name = (info.get("client") or "").strip() or None
        grouped.setdefault(name, {})[plate] = info
    return grouped


def _client_of_plate(plate, classification):
    return ((classification.get(plate) or {}).get("client") or "").strip() or None


_PLATE_OWNER_PATH = os.path.join(os.path.dirname(__file__), "data", "fleet_today.json")
_plate_owner_cache = {"mtime": None, "map": {}}


def client_for_plate(plate):
    """
    Which client owns this vehicle, for the per-vehicle emails that only
    ever receive a plate (on_comment_added, the reconnect check, the
    recovery notice).

    Read from the dashboard payload rather than threaded through every
    caller: the alternative is adding a client argument to each of them
    and to every one of their call sites, where a single missed site
    silently reverts to mailing everyone - the exact failure being
    fixed. Doing the lookup here means a caller cannot forget.

    Returns None when the plate is unknown or unmapped, which callers
    MUST treat as "do not send to clients" rather than "send to all".
    """
    try:
        mtime = os.path.getmtime(_PLATE_OWNER_PATH)
    except OSError:
        return None
    if _plate_owner_cache["mtime"] != mtime:
        try:
            with open(_PLATE_OWNER_PATH) as f:
                rows = json.load(f).get("full") or []
            _plate_owner_cache.update({
                "mtime": mtime,
                "map": {str(r.get("plate", "")).strip(): (r.get("client") or "").strip() or None
                        for r in rows if isinstance(r, dict) and r.get("plate")},
            })
        except (ValueError, OSError):
            return None
    return _plate_owner_cache["map"].get(str(plate).strip())


def _send_per_client(classification, digest_key, interval_days, build, recipients=None):
    """
    Shared driver for every client-facing digest: one email per client,
    containing only that client's vehicles, to only that client's
    recipients.

    build(client, subset, to) returns (subject, html, preheader, count),
    or None when that client has nothing worth sending this cycle.

    The interval gate is keyed PER CLIENT ("<digest>:<client>"). A
    single shared key would mean the first client sent suppresses every
    other client's digest for the whole interval - they'd each get mail
    roughly once per N clients instead of once per interval, and which
    client won would depend on dict ordering. One client failing to
    send (no recipients, SMTP error) likewise must not consume the
    gate for the others, so the timestamp is only written on success.

    Note this resets each digest's clock once, on first run after
    deploy: the old un-keyed timestamps no longer match. One extra
    digest is a much better failure than a silent month of none.
    """
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0, "perClient": {}}

    grouped = _clients_of(classification)
    if not grouped:
        result["reason"] = "no vehicles"
        return result

    for client in sorted(k for k in grouped if k):
        subset = grouped[client]
        key = f"{digest_key}:{client}"
        try:
            last_sent = sheets_store.get_digest_last_sent(key)
            if last_sent and (datetime.now() - last_sent).days < interval_days:
                result["perClient"][client] = "interval not elapsed"
                continue
        except Exception as e:
            result["perClient"][client] = f"could not check last-sent, skipping to avoid spamming: {e}"
            continue

        to = (recipients or _client_recipients)(client)
        if not to:
            result["perClient"][client] = "no recipients on file for this client"
            continue

        built = build(client, subset, to)
        if built is None:
            result["perClient"][client] = "nothing to report"
            continue
        subject, html, preheader, count = built

        message_id, err = mailer.send(to, subject, html, preheader)
        if err:
            result["perClient"][client] = err
            continue
        try:
            sheets_store.set_digest_last_sent(key)
        except Exception as e:
            print(f"{digest_key} sent to {client} but could not record last-sent: {e}")
        result["sent"] = True
        result["count"] += count
        result["perClient"][client] = f"sent, {count} vehicle(s)"

    # Vehicles nobody can be emailed about are worth saying out loud -
    # silently skipping them looks identical to there being none.
    unmapped = grouped.get(None)
    if unmapped:
        print(f"{digest_key}: {len(unmapped)} vehicle(s) have no client mapping and were not "
              f"included in any client digest - check the platform-account mapping.")
    if not result["sent"] and not result["reason"]:
        result["reason"] = "; ".join(f"{c}: {r}" for c, r in result["perClient"].items()) or "no clients"
    return result


def _staff_recipients(client=None):
    """
    Internal recipients. With a client, that means every admin (admin
    sees all clients by definition - permissions.ALL_CLIENTS_ROLES) plus
    only the technicians assigned to that client, mirroring exactly what
    each of them can already open in the dashboard. Without one, every
    admin and technician, for mail that isn't about a particular fleet.
    """
    if client is None:
        return [r["email"] for r in notification_recipients(roles=("admin", "technician"))]
    seen, out = set(), []
    for r in (notification_recipients(roles=("admin",))
              + notification_recipients(roles=("technician",), client=client)):
        if r["email"].lower() not in seen:
            seen.add(r["email"].lower())
            out.append(r["email"])
    return out


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
    # Only this vehicle's own client, never every client - see
    # _client_recipients(). An unmapped plate has no defensible client
    # audience, so it falls through to staff-only below rather than
    # being broadcast.
    owning_client = client_for_plate(plate)

    if role in ("admin", "technician") and requires_followup is not False and owning_client:
        to = _client_recipients(owning_client)
        if not to:
            result["reason"] = f"no client recipients on file for {owning_client}"
            return result
        no_url = (respond_urls or {}).get("no_followup", "#")
        need_url = (respond_urls or {}).get("needs_attention", "#")
        html, preheader = email_templates.build_update_email(
            plate, comment, added_by, role, timestamp, no_url, need_url, recent,
            reopened=case["reopened"], previous_closed_at=case["previousClosedAt"],
            client=owning_client)
        return _send_and_record(plate, case, to, html, preheader, result)

    if role in ("client", "admin", "technician"):
        to = list({*_staff_recipients(owning_client),
                   *(_client_recipients(owning_client) if owning_client else [])})
        if not to:
            result["reason"] = "no recipients on file"
            return result
        html, preheader = email_templates.build_outcome_email(
            plate, comment, added_by, requires_followup, timestamp, recent,
            reopened=case["reopened"], previous_closed_at=case["previousClosedAt"],
            client=owning_client)
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
    from datetime import datetime
    result = {"sent": False, "reason": None, "count": 0, "perClient": {}}
    if not recovered:
        result["reason"] = "nothing recovered this cycle"
        return result

    # One notice per client, listing only their own recoveries. Sending
    # a single combined notice would tell every client which of every
    # other client's vehicles had been offline and where they are.
    by_client = {}
    for plate in recovered:
        info = classification.get(plate) or {}
        client = (info.get("client") or "").strip() or None
        by_client.setdefault(client, []).append((plate, info))

    for client in sorted(k for k in by_client if k):
        entries = by_client[client]
        to = list({*_staff_recipients(client), *_client_recipients(client)})
        if not to:
            result["perClient"][client] = "no recipients on file"
            continue
        vehicles = [{
            "plate": plate,
            "location": info.get("last_location") or "Unknown",
            "comment": (info.get("feedback") or {}).get("comment") if info.get("feedback") else None,
        } for plate, info in entries]
        timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_recovery_notice_email(vehicles, timestamp, client=client)
        message_id, err = mailer.send(to, f"{client} - back online: {len(vehicles)} vehicle(s)",
                                      html, preheader)
        if err:
            result["perClient"][client] = err
            continue
        result["sent"] = True
        result["count"] += len(vehicles)
        result["perClient"][client] = f"sent, {len(vehicles)} vehicle(s)"

    unmapped = by_client.get(None)
    if unmapped:
        print(f"Recovery notice: {len(unmapped)} recovered vehicle(s) have no client mapping "
              f"and were not included in any notice.")
    if not result["sent"] and not result["reason"]:
        result["reason"] = "; ".join(f"{c}: {r}" for c, r in result["perClient"].items()) or "no clients"
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

    def build(client, subset, to):
        pending = sorted(
            (plate, info) for plate, info in subset.items()
            if info.get("status") == "Pending Customer Confirmation"
        )
        critical_no_feedback = sorted(
            (plate, info) for plate, info in subset.items()
            if info.get("status") == "Technical Escalation" and info.get("feedback") is None
        )
        if not pending and not critical_no_feedback:
            return None

        vehicles = [_vehicle_dict(p, i, overdue=(i.get("days_silent") or 0) >= overdue_days) for p, i in pending]
        critical_vehicles = [_vehicle_dict(p, i) for p, i in critical_no_feedback]
        timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_pending_confirmation_digest_email(
            vehicles, timestamp, critical_no_feedback=critical_vehicles, client=client)
        total = len(vehicles) + len(critical_vehicles)
        return (f"Weekly check-in: {total} vehicle(s) need your input", html, preheader, total)

    return _send_per_client(classification, "pending_confirmation", interval_days, build)


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
    import respond_tokens

    def build(client, subset, to):
        known = sorted(
            (plate, info) for plate, info in subset.items()
            if info.get("status") == "Known Issue"
        )
        if not known:
            return None
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
        html, preheader = email_templates.build_known_issues_checkin_digest_email(vehicles, timestamp, client=client)
        return (f"Quick check-in: please reconfirm {len(vehicles)} known issue(s)",
                html, preheader, len(vehicles))

    return _send_per_client(classification, "known_issues_checkin", interval_days, build)


def send_technical_escalation_digest(classification, base_url, interval_days):
    """Staff-facing counterpart to send_pending_confirmation_digest -
    same interval-gated snapshot mechanism, different audience and no
    respond-token links (staff act from the dashboard directly, not by
    clicking an email button - see the "Open in dashboard" deep link,
    handled by templates/dashboard.html's boot())."""
    from datetime import datetime

    def build(client, subset, to):
        escalated = sorted(
            (plate, info) for plate, info in subset.items()
            if info.get("status") == "Technical Escalation"
        )
        if not escalated:
            return None
        vehicles = [
            {
                "plate": plate, "detail": _offline_platform_detail(info),
                "severity": info.get("severity") or "Escalated",
                "dashboard_url": f"{base_url}/?plate={plate}" if base_url else "",
            }
            for plate, info in escalated
        ]
        timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_technical_escalation_digest_email(vehicles, timestamp, client=client)
        return (f"{client} - weekly check-in: {len(vehicles)} vehicle(s) in Technical Escalation",
                html, preheader, len(vehicles))

    # Staff audience, but still per client: a technician assigned only to
    # GTL has no more business receiving AGL's fault list by email than
    # they do opening it in the dashboard. Admins are on every one of
    # these, so nothing stops reaching someone.
    return _send_per_client(classification, "technical_escalation", interval_days, build,
                            recipients=_staff_recipients)


def _split_tampering_by_client(tampering, classification):
    """
    Slices the tampering result into one per client.

    tamper_engine works off the raw report files and knows nothing about
    clients, so every case has to be attributed by resolving its plate
    through the classification. Summary counts and severity bands are
    recomputed per client rather than reused - the fleet-wide totals
    would otherwise tell each client how many cases exist across
    everyone else's vehicles.
    """
    owner = {plate: (info.get("client") or "").strip() or None
             for plate, info in classification.items()}
    per = {}

    def bucket(client):
        return per.setdefault(client, {"confirmed": [], "unconfirmed": [], "top_vehicles": [],
                                       "checked_assets": [], "severity_bands": [], "summary": {}})

    for key in ("confirmed", "unconfirmed", "top_vehicles", "checked_assets"):
        for case in tampering.get(key, []) or []:
            client = owner.get(str(case.get("plate", "")).strip())
            if client:
                bucket(client)[key].append(case)

    for client, data in per.items():
        confirmed, unconfirmed = data["confirmed"], data["unconfirmed"]
        data["summary"] = {
            **{k: v for k, v in (tampering.get("summary") or {}).items()
               if k in ("period_label",)},
            "confirmed": len(confirmed), "unconfirmed": len(unconfirmed),
        }
        bands = []
        for band in tampering.get("severity_bands", []) or []:
            name = band.get("severity")
            bands.append({**band,
                          "confirmed": sum(1 for c in confirmed if c.get("Severity") == name
                                           or c.get("severity") == name),
                          "unconfirmed": sum(1 for c in unconfirmed if c.get("Severity") == name
                                             or c.get("severity") == name)})
        data["severity_bands"] = bands
    return per


def send_tamper_risk_report_digest(tampering, base_url, interval_days, classification=None):
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
    if classification is None:
        # Without the classification there is no way to tell whose
        # vehicles these cases belong to, and the previous behaviour -
        # mail the lot to every client contact - is exactly the leak
        # this is fixing. Refuse rather than guess.
        return {"sent": False, "count": 0,
                "reason": "classification not supplied - cannot attribute tampering cases to a client"}

    per_client = _split_tampering_by_client(tampering, classification)

    def build(client, subset, to):
        data = per_client.get(client)
        if not data:
            return None
        summary = data["summary"]
        if not summary.get("confirmed") and not summary.get("unconfirmed"):
            return None
        timestamp = datetime.now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_tamper_risk_report_email(
            summary, data["severity_bands"], data["top_vehicles"], data["checked_assets"], timestamp,
            client=client)
        return (f"{client} - tampering report: {summary.get('confirmed', 0)} confirmed, "
                f"{summary.get('unconfirmed', 0)} unconfirmed",
                html, preheader, len(data["top_vehicles"]))

    return _send_per_client(classification, "tamper_risk_report", interval_days, build)


def _send_reconnect_check(plate, days_offline, open_comment, base_url):
    from datetime import datetime
    import respond_tokens

    if not base_url:
        # Respond links with no domain are dead links - worse than not
        # sending at all, since a client clicking one lands nowhere.
        print(f"Reconnect-check for {plate} skipped: no base URL configured "
              f"(set PUBLIC_BASE_URL, or run on Render where it's automatic).")
        return False

    # Only this vehicle's own client. An unmapped plate has no
    # defensible client audience, so nothing is sent rather than
    # everything being sent to everyone.
    owning_client = client_for_plate(plate)
    if not owning_client:
        print(f"Reconnect-check for {plate} skipped: no client mapping for this vehicle.")
        return False
    to = _client_recipients(owning_client)
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
        plate, days_offline, open_comment, timestamp, no_url, need_url, recent,
        client=owning_client)

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
