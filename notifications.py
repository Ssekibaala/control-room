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
from datetime import datetime, timedelta

import sheets_store
import mailer
import email_templates
from users import notification_recipients


def _now():
    """
    One clock for every send decision in this module.

    The digest gate used to compare datetime.now() (whatever the
    container's local timezone happens to be - UTC on Northflank)
    against stamps that other parts of this codebase write with
    now_eat(). Self-consistent as long as nothing else touched the same
    values, but "was this week's slot already used" is now answered
    against a wall-clock day and hour, and a three-hour offset between
    the clock that decides the slot and the clock a human reads off
    settings.ini would put the weekly check-in out at 5am rather than
    8am. Falls back to the naive local clock only if fleet_logic isn't
    importable, which in practice means a bare unit-test import.
    """
    try:
        from schema import now_eat
        return now_eat().replace(tzinfo=None)
    except Exception:
        return datetime.now()


def weekly_slot_start(now=None, send_days=None, send_time=(8, 0), window_hours=6):
    """
    The start of the currently-open weekly send slot, or None if we are
    not inside one.

    A slot opens at send_time on each configured weekday and stays open
    for window_hours. This replaced an elapsed-days comparison whose
    .days floor pushed the send one day later every time a cycle ran
    slightly earlier than the previous week's - see settings.py's
    [digests] comment. Returning the slot's START (not just True) is
    what makes the "already sent this slot" check exact: a send is due
    only when the last recorded send is older than the slot we're in,
    so any number of runs inside the same window send exactly once,
    and no run outside it sends at all.
    """
    now = now or _now()
    send_days = set(range(7)) if send_days is None else set(send_days)
    hour, minute = send_time
    # Check today's slot and yesterday's - a window that runs past
    # midnight (say 22:00 + 6h) is still the same slot at 01:00 the
    # next morning, and dropping it there would silently lose every
    # late-evening schedule.
    for days_back in (0, 1):
        day = (now - timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if day.weekday() not in send_days:
            continue
        if day <= now < day + timedelta(hours=window_hours):
            return day
    return None


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


def _send_per_client(classification, digest_key, slot_start, build, recipients=None,
                     force=False, override_to=None, record=True, only_client=None):
    """
    Shared driver for every client-facing digest: one email per client,
    containing only that client's vehicles, to only that client's
    recipients.

    build(client, subset, to) returns (subject, html, preheader, count),
    or None when that client has nothing worth sending this cycle.

    slot_start is the start of the currently-open weekly send window
    (see weekly_slot_start()), or None when we are outside one. A
    client's digest goes out when its last recorded send is older than
    that slot - which means every run inside one window sends exactly
    once, and a run outside any window sends nothing at all. force=True
    is the dashboard's "Send check-in now" button and skips the gate
    entirely; it does not skip anything else.

    The gate is keyed PER CLIENT ("<digest>:<client>"). A single shared
    key would mean the first client sent suppresses every other
    client's digest for the whole week - they'd each get mail roughly
    once per N clients, and which client won would depend on dict
    ordering. One client failing to send (no recipients, SMTP error)
    likewise must not consume the gate for the others, so the timestamp
    is only written on success.

    override_to/record=False back the ad-hoc "send this to one named
    address" path (send_manual_report): the mail goes to exactly that
    address and the weekly last-sent stamp is deliberately left alone,
    so forwarding a copy to somebody can never suppress the real
    scheduled send to the actual recipients.
    """
    result = {"sent": False, "reason": None, "count": 0, "perClient": {}}

    grouped = _clients_of(classification)
    if not grouped:
        result["reason"] = "no vehicles"
        return result

    if not force and slot_start is None:
        result["reason"] = "outside the configured weekly send window"
        return result

    if only_client is not None and only_client not in grouped:
        # Named explicitly by the ad-hoc send path, so "no clients" would
        # be a misleading way to report it - the client exists, it just
        # has no vehicles in this snapshot.
        result["reason"] = f"no vehicles on file for {only_client}"
        return result

    for client in sorted(k for k in grouped if k):
        if only_client is not None and client != only_client:
            continue
        subset = grouped[client]
        key = f"{digest_key}:{client}"
        if not force:
            try:
                last_sent = sheets_store.get_digest_last_sent(key)
                if last_sent and last_sent >= slot_start:
                    result["perClient"][client] = "already sent in this window"
                    continue
            except Exception as e:
                result["perClient"][client] = f"could not check last-sent, skipping to avoid spamming: {e}"
                continue

        to = list(override_to) if override_to else (recipients or _client_recipients)(client)
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
        if record:
            try:
                sheets_store.set_digest_last_sent(key, when=_now())
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

    timestamp = _now().strftime("%d %b %Y, %H:%M")

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

    # entry_type was accepted here and then never looked at, so a
    # Recommended Action - which app.py's /api/feedback documents as
    # "the technician's operational instruction to the field", refuses
    # to let a client submit at all, and permissions.py strips out of
    # every client-facing payload - was emailed straight to that client
    # anyway, formatted as a question for them to answer. It is an
    # internal job-card note. Staff get it; the client does not, exactly
    # as everywhere else in this codebase already treats it.
    internal_only = str(entry_type or "").strip().lower() == "action"
    if internal_only:
        to = _staff_recipients(owning_client)
        if not to:
            result["reason"] = "no staff recipients on file for an internal action note"
            return result
        html, preheader = email_templates.build_internal_action_email(
            plate, comment, added_by, role, timestamp, recent, client=owning_client,
            dashboard_url=f"{email_templates._public_base_url()}/?plate={plate}"
            if email_templates._public_base_url() else "")
        return _send_and_record(plate, case, to, html, preheader, result)

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


def check_reconnections(classification, recovered, base_url="", episodes=None):
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
    episodes = episodes or {}
    for plate in recovered:
        info = classification.get(plate) or {}
        fb = info.get("feedback")
        if not fb or fb.get("requiresFollowup") is not True:
            continue  # nothing outstanding to check on for this plate
        try:
            if _send_reconnect_check(plate, info.get("days_silent") or 0, fb["comment"], base_url,
                                     episode=episodes.get(plate)):
                prompted.append(plate)
        except Exception as e:
            print(f"Reconnect-check email failed for {plate}: {e}")
    return prompted


_RECOVERY_LEDGER_PATH = os.path.join(os.path.dirname(__file__), "data", "recovery_notified.json")


def _recovery_ledger():
    try:
        with open(_RECOVERY_LEDGER_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_recovery_notified(entries):
    """
    Remembers {plate: episode key} for every recovery just emailed about.

    The primary guard against a repeat is that the next cycle diffs
    against a vehicle status where this plate is already Online, so it
    is no longer a transition at all. But that write can fail
    (sheets_store.save_vehicle_status is wrapped in a try/except in
    run_import precisely because losing it must not kill the import),
    and when it does, the SAME recovery is rediscovered next cycle and
    mailed again. Keying on the episode - which offline spell this
    recovery ended - means a genuine second offline/online cycle still
    notifies, while the same one never notifies twice.
    """
    ledger = _recovery_ledger()
    ledger.update(entries)
    try:
        os.makedirs(os.path.dirname(_RECOVERY_LEDGER_PATH), exist_ok=True)
        with open(_RECOVERY_LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2)
    except OSError as e:
        print(f"Could not record which recoveries were notified ({e}); "
              f"a failed status write could cause one repeat notice.")


def _episode_key(plate, episode):
    """Identifies one offline spell. offlineSince is the natural key -
    a vehicle that goes offline again gets a new one."""
    return f"{plate}@{(episode or {}).get('offlineSince') or 'unknown'}"


def _humanise_duration(hours):
    """
    How long an outage lasted, in the units a reader would use.

    Days-and-hours rather than a decimal for anything under a
    fortnight: "3.2 days" makes the reader do the arithmetic to get to
    "since Saturday morning", which is the thing they actually want to
    know. Past two weeks the hours stop carrying information and whole
    days are the honest resolution.
    """
    if hours is None:
        return None
    if hours < 1:
        minutes = max(1, int(round(hours * 60)))
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if hours < 24:
        whole = int(hours)
        rem = int(round((hours - whole) * 60))
        return f"{whole} hour{'s' if whole != 1 else ''}" + (f" {rem} min" if rem else "")
    days, rem_hours = int(hours // 24), int(round(hours % 24))
    if rem_hours == 24:  # rounding up from e.g. 47.7h must roll the day over, not print "1 day 24 hours"
        days, rem_hours = days + 1, 0
    if days >= 14 or rem_hours == 0:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''} {rem_hours} hour{'s' if rem_hours != 1 else ''}"


def send_recovery_notice(classification, recovered, episodes=None, requires_comment=True,
                         skip_plates=()):
    """
    Informational notice for vehicles that came back online this cycle.

    `episodes` is {plate: {offlineSince, offlinePlatforms, lastSeenAt,
    offlineHours}} from run_import._day_over_day - the offline spell
    that just ended. Without it this email could only say "back
    online", which is the least useful half of the news: the questions
    a reader actually has are how long it was gone, and which platform
    stopped reporting. Both are already known at the moment the
    transition is detected, they just were not being carried across.

    requires_comment gates WHICH recoveries are worth an unsolicited
    email (settings.ini [recovery] requires_comment). A fleet this size
    reconnects constantly, and mailing every one of them is how the
    client learns to filter these away - at which point the ones that
    matter are lost too. A comment on file is somebody having already
    said this vehicle matters, which makes it a per-asset control with
    no second list to keep in sync.

    skip_plates are the vehicles that already got the two-button
    reconnect-check email for this same reconnection
    (check_reconnections). Two emails about one event, arriving
    together, is the most avoidable kind of noise there is.

    No schedule gate: this is a report of THIS cycle's transitions, not
    a snapshot of ongoing state, so a schedule would just mean the
    recoveries in between are never mentioned at all.
    """
    result = {"sent": False, "reason": None, "count": 0, "perClient": {}, "skipped": {}}
    if not recovered:
        result["reason"] = "nothing recovered this cycle"
        return result

    episodes = episodes or {}
    ledger = _recovery_ledger()
    skip_plates = set(skip_plates)

    # One notice per client, listing only their own recoveries. Sending
    # a single combined notice would tell every client which of every
    # other client's vehicles had been offline and where they are.
    by_client = {}
    for plate in recovered:
        info = classification.get(plate) or {}
        if plate in skip_plates:
            result["skipped"][plate] = "already asked about in the reconnect-check email"
            continue
        if requires_comment and not info.get("feedback"):
            result["skipped"][plate] = "no comment on file (recovery.requires_comment is on)"
            continue
        if ledger.get(plate) == _episode_key(plate, episodes.get(plate)):
            result["skipped"][plate] = "this same recovery was already notified"
            continue
        client = (info.get("client") or "").strip() or None
        by_client.setdefault(client, []).append((plate, info))

    if not any(k for k in by_client if k):
        result["reason"] = "no recovery this cycle met the notification rules"
        return result

    notified = {}
    for client in sorted(k for k in by_client if k):
        entries = by_client[client]
        to = list({*_staff_recipients(client), *_client_recipients(client)})
        if not to:
            result["perClient"][client] = "no recipients on file"
            continue
        vehicles = []
        for plate, info in entries:
            episode = episodes.get(plate) or {}
            vehicles.append({
                "plate": plate,
                "location": info.get("last_location") or "Unknown",
                "comment": (info.get("feedback") or {}).get("comment") if info.get("feedback") else None,
                "offline_since": episode.get("offlineSince"),
                "offline_platforms": episode.get("offlinePlatforms") or [],
                "offline_for": _humanise_duration(episode.get("offlineHours")),
                "last_seen": episode.get("lastSeenAt"),
            })
        timestamp = _now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_recovery_notice_email(vehicles, timestamp, client=client)
        message_id, err = mailer.send(to, f"{client} - back online: {len(vehicles)} vehicle(s)",
                                      html, preheader)
        if err:
            result["perClient"][client] = err
            continue
        for plate, _ in entries:
            notified[plate] = _episode_key(plate, episodes.get(plate))
        result["sent"] = True
        result["count"] += len(vehicles)
        result["perClient"][client] = f"sent, {len(vehicles)} vehicle(s)"

    if notified:
        _record_recovery_notified(notified)

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


def send_pending_confirmation_digest(classification, base_url, slot_start, overdue_days, **opts):
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

    Gated on the configured weekly send window
    (notifications.weekly_slot_start + sheets_store.get_digest_last_sent/
    set_digest_last_sent) - escalation to Technical Escalation itself is
    untouched by any of this, it stays exactly the days-based rule in
    classifier.py it always was. This is an additive notification layer,
    not a gate.
    """
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
        timestamp = _now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_pending_confirmation_digest_email(
            vehicles, timestamp, critical_no_feedback=critical_vehicles, client=client)
        total = len(vehicles) + len(critical_vehicles)
        # Client-named, like every other digest subject here. Without it
        # a contact who works with more than one of these fleets gets
        # two identically-titled weekly emails and has to open both to
        # find out which is which - and Gmail threads them together.
        return (f"{client} - weekly check-in: {total} vehicle(s) need your input", html, preheader, total)

    return _send_per_client(classification, "pending_confirmation", slot_start, build, **opts)


def send_known_issues_checkin_digest(classification, base_url, slot_start, **opts):
    """
    Client-facing counterpart to send_pending_confirmation_digest: a
    weekly re-confirmation of every vehicle currently marked Known
    Issue (classifier.py's known_issue override - an earlier explicit
    "no follow-up needed"). That's a point-in-time judgement, not a
    permanent fact - the same problem could recur, or the client's
    circumstances could have changed - so this keeps asking rather than
    treating one old answer as settled forever. Same snapshot-not-
    transition shape and window-gating as the other two digests.
    """
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
        timestamp = _now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_known_issues_checkin_digest_email(vehicles, timestamp, client=client)
        return (f"{client} - quick check-in: please reconfirm {len(vehicles)} known issue(s)",
                html, preheader, len(vehicles))

    return _send_per_client(classification, "known_issues_checkin", slot_start, build, **opts)


def send_technical_escalation_digest(classification, base_url, slot_start, **opts):
    """Staff-facing counterpart to send_pending_confirmation_digest -
    same window-gated snapshot mechanism, different audience and no
    respond-token links (staff act from the dashboard directly, not by
    clicking an email button - see the "Open in dashboard" deep link,
    handled by templates/dashboard.html's boot())."""

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
        timestamp = _now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_technical_escalation_digest_email(vehicles, timestamp, client=client)
        return (f"{client} - weekly check-in: {len(vehicles)} vehicle(s) in Technical Escalation",
                html, preheader, len(vehicles))

    # Staff audience, but still per client: a technician assigned only to
    # GTL has no more business receiving AGL's fault list by email than
    # they do opening it in the dashboard. Admins are on every one of
    # these, so nothing stops reaching someone.
    return _send_per_client(classification, "technical_escalation", slot_start, build,
                            recipients=_staff_recipients, **opts)


def _case_plate(case):
    """
    The plate on a tampering case, whichever spelling this particular
    list uses.

    These four lists reach here through three different shapes:
    confirmed/unconfirmed are loader-shaped ("Plate", capital P - see
    run_import._tamper_case_to_loader_shape), checked_assets is built by
    hand as lowercase "plate" (run_import._checked_assets_summary), and
    top_vehicles carried NO plate at all until run_import.
    _top_vehicles_from_tamper_result was given one. Reading only
    "plate" therefore resolved every confirmed and unconfirmed case to
    the empty string, every client bucket came out empty, the per-client
    summary was 0/0, build() returned None for everyone, and the weekly
    tampering report silently never sent to anybody - reported as
    "nothing to report", which is indistinguishable from a genuinely
    clean week. Accepting both spellings is what makes that impossible
    to reintroduce by adding a fifth list in a fourth shape.
    """
    for key in ("plate", "Plate"):
        value = str(case.get(key) or "").strip()
        if value:
            return value
    return ""


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
    unattributed = {}

    def bucket(client):
        return per.setdefault(client, {"confirmed": [], "unconfirmed": [], "top_vehicles": [],
                                       "checked_assets": [], "severity_bands": [], "summary": {}})

    for key in ("confirmed", "unconfirmed", "top_vehicles", "checked_assets"):
        for case in tampering.get(key, []) or []:
            plate = _case_plate(case)
            client = owner.get(plate)
            if client:
                bucket(client)[key].append(case)
            else:
                unattributed.setdefault(key, []).append(plate or "<no plate on case>")

    # Said out loud rather than silently dropped: a case whose plate
    # doesn't resolve to a client is the exact failure this function
    # already had once, and "0 cases" looks identical to "a clean week"
    # from the outside.
    for key, plates in unattributed.items():
        print(f"Tampering report: {len(plates)} {key} case(s) could not be attributed to a client "
              f"and are in nobody's report - e.g. {', '.join(sorted(set(plates))[:5])}")

    for client, data in per.items():
        confirmed, unconfirmed = data["confirmed"], data["unconfirmed"]
        data["summary"] = {
            **{k: v for k, v in (tampering.get("summary") or {}).items()
               if k in ("period_label",)},
            "confirmed": len(confirmed), "unconfirmed": len(unconfirmed),
            # This client's own gap count, NOT the fleet-wide
            # summary["gaps_checked"]. The email's opening sentence reads
            # "out of N location gaps checked this week", and the
            # per-client summary never carried gaps_checked at all, so
            # that N rendered as 0 - "out of 0 gaps checked, 3 are
            # confirmed", which is both wrong and obviously wrong to the
            # reader. Their own confirmed+unconfirmed is the only figure
            # here that is genuinely theirs: the fleet-wide denominator
            # counts every other client's gaps too and cannot be shown.
            "gaps_checked": len(confirmed) + len(unconfirmed),
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


def send_tamper_risk_report_digest(tampering, base_url, slot_start, classification=None, **opts):
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
        timestamp = _now().strftime("%d %b %Y, %H:%M")
        html, preheader = email_templates.build_tamper_risk_report_email(
            summary, data["severity_bands"], data["top_vehicles"], data["checked_assets"], timestamp,
            client=client)
        # Cases, not top_vehicles: top_vehicles is only the worst
        # offenders singled out for a physical check and is legitimately
        # empty on a week with no confirmed case at all, which made an
        # otherwise fine send log "sent, 0 vehicle(s)".
        count = summary.get("confirmed", 0) + summary.get("unconfirmed", 0)
        return (f"{client} - tampering report: {summary.get('confirmed', 0)} confirmed, "
                f"{summary.get('unconfirmed', 0)} unconfirmed",
                html, preheader, count)

    return _send_per_client(classification, "tamper_risk_report", slot_start, build, **opts)


def _send_reconnect_check(plate, days_offline, open_comment, base_url, episode=None):
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
    timestamp = _now().strftime("%d %b %Y, %H:%M")
    episode = episode or {}
    html, preheader = email_templates.build_reconnect_check_email(
        plate, days_offline, open_comment, timestamp, no_url, need_url, recent,
        client=owning_client,
        offline_since=episode.get("offlineSince"),
        offline_platforms=episode.get("offlinePlatforms") or [],
        offline_for=_humanise_duration(episode.get("offlineHours")))

    result = {"sent": False, "reason": None, "case_id": case["caseId"]}
    sent = _send_and_record(plate, case, to, html, preheader, result)
    return sent["sent"]


# ---- Ad-hoc sends ----------------------------------------------------
# "Email this to one named person, now." Everything above decides its own
# audience from the registry; this decides nothing and sends exactly
# where it's told. That is the whole point - a driver's supervisor, an
# insurer, somebody's colleague who isn't and shouldn't be an account on
# this platform.
#
# Two rules make it safe to have at all:
#   - it NEVER writes the weekly last-sent stamp (record=False), so
#     forwarding a copy of the check-in to somebody on Wednesday cannot
#     suppress the real Monday send to the real recipients. That
#     interaction is the obvious way a convenience feature like this
#     quietly breaks the scheduled one.
#   - the caller has already checked that the requester may see this
#     client/plate (app.py's _plate_allowed / _assigned_clients). This
#     function does not re-derive permission from anything the request
#     supplied, because the request is what's being constrained.

MANUAL_REPORTS = ("weekly_checkin", "critical_assets", "known_issues", "tamper_report", "asset")


def send_manual_report(reports, to_addrs, classification, base_url, overdue_days=2,
                       tampering=None, client=None, plate=None, note=None, sent_by=""):
    """
    reports is any subset of MANUAL_REPORTS. Returns
    {report: result-dict} so the caller can report per-report outcomes
    rather than one opaque ok/failed for a request that may have asked
    for four different things.

    client scopes the digest-shaped reports to one fleet. It is
    effectively required for them: without it every client's digest
    would be sent to this one address, which is the same cross-client
    leak _client_recipients exists to prevent, just aimed at a manually
    typed address instead of a registry one.
    """
    to_addrs = [a for a in (to_addrs or []) if a]
    out = {}
    if not to_addrs:
        return {"error": "no recipient address given"}

    common = {"force": True, "override_to": to_addrs, "record": False, "only_client": client}

    for report in reports:
        if report == "weekly_checkin":
            out[report] = send_pending_confirmation_digest(
                classification, base_url, None, overdue_days, **common)
        elif report == "critical_assets":
            out[report] = send_technical_escalation_digest(
                classification, base_url, None, **common)
        elif report == "known_issues":
            out[report] = send_known_issues_checkin_digest(
                classification, base_url, None, **common)
        elif report == "tamper_report":
            if tampering is None:
                out[report] = {"sent": False, "reason": "no tampering analysis available to send"}
            else:
                out[report] = send_tamper_risk_report_digest(
                    tampering, base_url, None, classification=classification, **common)
        elif report == "asset":
            out[report] = _send_asset_summary(plate, classification, to_addrs, base_url, note, sent_by)
        else:
            out[report] = {"sent": False, "reason": f"unknown report {report!r}"}

    # A note only makes sense attached to something; on a digest it would
    # have to be threaded through four different templates. Said out loud
    # rather than silently dropped.
    if note and "asset" not in reports:
        print("Manual send: a note was supplied but only the single-asset report carries one; it was not sent.")
    return out


def _send_asset_summary(plate, classification, to_addrs, base_url, note=None, sent_by=""):
    """
    Everything currently known about ONE vehicle, to one address: status,
    how long it has been silent and on which platforms, each platform's
    own last-seen time, last known location, and the full comment trail.

    Deliberately has no respond buttons. Those carry a signed token that
    can write feedback for this plate with no login, and this address
    was typed into a box by a member of staff - it hasn't been through
    the client registry, so it is not an address to hand a write
    credential to. Reading is what this is for.
    """
    result = {"sent": False, "reason": None}
    plate = (plate or "").strip()
    if not plate:
        result["reason"] = "no plate given for the single-asset report"
        return result
    info = classification.get(plate)
    if not info:
        result["reason"] = f"{plate} is not in the current fleet snapshot"
        return result

    history = []
    try:
        entries = sheets_store.load_feedback_cached().get(plate, {}).get("history", [])
        history = [
            {"comment": h["comment"], "addedBy": h["addedBy"], "requiresFollowup": h["requiresFollowup"],
             "date": h["date"].strftime("%d %b %Y, %H:%M") if h["date"].year > 1 else ""}
            for h in reversed(entries)
        ]
    except Exception as e:
        print(f"Manual asset report for {plate}: comment history unavailable ({e}), sending without it.")

    owning_client = (info.get("client") or "").strip() or None
    timestamp = _now().strftime("%d %b %Y, %H:%M")
    html, preheader = email_templates.build_asset_summary_email(
        plate, info, history, timestamp, client=owning_client, note=note, sent_by=sent_by,
        dashboard_url=f"{base_url}/?plate={plate}" if base_url else "",
        offline_detail=_offline_platform_detail(info),
        platform_seen=_platform_seen_rows(info))

    message_id, err = mailer.send(to_addrs, f"{plate} - vehicle status summary", html, preheader)
    if err:
        result["reason"] = err
        return result
    result["sent"] = True
    return result


def _platform_seen_rows(info):
    """Per-platform (name, Online/Offline, last report time) for the
    single-asset email - the same three numbers the dashboard's TLT/MIX/
    CAM columns show, which is what makes "offline" specific enough to
    act on rather than a bare label."""
    rows = []
    for platform, (status, seen) in sorted((info.get("platform_status") or {}).items()):
        rows.append({
            "platform": platform,
            "status": status,
            "seen": seen.strftime("%d %b %Y, %H:%M") if hasattr(seen, "strftime") else "No data",
        })
    return rows


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
