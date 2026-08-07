"""
Every tunable number in the system lives here, read from settings.ini
next to the EXE. Change the file, re-run, no code edit required.

If settings.ini is missing, sane defaults are used and the file is
auto-created so it's there to edit next time.
"""

import configparser
import os

DEFAULTS = {
    "thresholds": {
        "offline_threshold_days": "2",       # asset counts as offline after this many days silent
        "long_term_fault_days": "30",        # severity escalates to Critical after this many days
        "high_priority_days": "7",           # severity escalates to High after this many days
        "border_radius_km": "20",            # distance from a border point to count as border risk
    },
    "filters": {
        "ignore_demo_vehicles": "true",      # drop TMS demo / test-site rows
    },
    "mix_api": {
        # Live MiX Integrate API polling (replaces the mailed report
        # subscriptions, org by org).
        #
        # org_ids is now a LEGACY FALLBACK ONLY and is empty by design.
        # Which organisations get polled comes from the client registry
        # (client_registry.load_registry() -> the Sheets "Clients" tab),
        # so an org is always attached to a named client rather than
        # floating loose - that mapping is what makes per-client access
        # control and the dashboard's client filter possible at all.
        # Anything listed here is only used if Sheets AND the local
        # cache are both unavailable, and lands under one synthetic
        # "Unassigned" client. Leave it empty unless you are
        # deliberately running without Sheets.
        #
        # poll_interval_minutes is how often the in-process poller (see
        # app.py) re-fetches while the app is running; it does NOT
        # persist across restarts/dyno sleep, so this is a best-effort
        # cadence, not a guaranteed one.
        "org_ids": "",
        "poll_interval_minutes": "5",
        "inter_org_delay_seconds": "5",
    },
    "teletrac_api": {
        # Live Teletrac Integrate API polling (replaces the mailed
        # "Bulk - Teletrac platform offline report" CSV, client by
        # client - same shape as [mix_api] above). client_ids is
        # comma-separated and empty by default - the poller does
        # nothing until clients are listed here.
        "client_ids": "",
        "poll_interval_minutes": "5",
        "inter_client_delay_seconds": "5",
    },
    "ft_cloud_api": {
        # Live FT Cloud OpenAPI polling (replaces the mailed "Offline
        # Vehicle Reports" zip, fleet by fleet - same shape as the two
        # sections above). fleet_ids is comma-separated and empty by
        # default - the poller does nothing until fleets are listed.
        #
        # position_source is the important one. FT exposes no bulk
        # position endpoint at all, so coordinates come from one of:
        #   webhook - FT pushes GPS to this app's own receiver route.
        #             Costs ZERO extra API calls per poll and is
        #             real-time rather than poll-interval-stale. Needs
        #             a one-off subscribe (see /api/ftcloud/webhook/
        #             subscribe) and a reachable public URL.
        #   trips   - fall back to reading each device's most recent
        #             trip. Correct, but one HTTP call PER DEVICE per
        #             cycle (55 for GTL today vs 3 for everything
        #             else). Use when the webhook can't be reached.
        #   none    - skip coordinates entirely; online/offline still
        #             works, border-risk loses its input.
        "fleet_ids": "",
        "poll_interval_minutes": "5",
        "inter_call_delay_seconds": "0.2",
        "position_source": "webhook",
        "position_lookback_days": "3",
        # A pushed coordinate older than this is treated as no
        # coordinate - a stale-but-present fix would otherwise look
        # current to the border-risk distance check.
        "webhook_position_max_age_minutes": "1440",
    },
    "digests": {
        # Weekly rollup emails, not per-vehicle transition emails - one
        # message listing every currently-pending/escalated vehicle.
        # Escalation itself stays purely days-based (see classifier.py);
        # these are an additive notification layer, not a gate on
        # anything.
        #
        # WHEN they go out is a named day and time of day, not "at least
        # N days since the last one". The elapsed-days rule this
        # replaced compared (now - last_sent).days against 7, and
        # .days floors: a cycle that ran even a minute earlier than the
        # previous week's saw 6, skipped, and pushed the send to the
        # next day - so the weekly check-in walked forward through the
        # week instead of landing on the same morning. A named slot
        # cannot drift, and is what "configurable time and date of the
        # day" actually needs.
        #
        # send_day accepts a weekday name (monday..sunday), or "daily"
        # to run every day at send_time, or a comma-separated list
        # ("monday,thursday") for more than one slot a week.
        "weekly_send_day": "monday",
        "weekly_send_time": "08:00",      # 24h, East Africa Time (same clock as everything else here)
        # How long after send_time the slot stays open. The import that
        # sends these is not a precise clock - it's whichever poll or
        # scheduled run happens next - so the slot has to be a window,
        # not an instant. Anything inside the window sends once; the
        # per-client last-sent stamp closes it for the rest of the day.
        "weekly_send_window_hours": "6",
        "pending_confirmation_overdue_days": "2",  # visual "overdue" flag inside the client digest only
        # Which of the four rollups are on. Turning one off is a
        # deliberate, visible setting rather than commenting out a call.
        "send_pending_digest": "true",           # client: vehicles in Pending Customer Confirmation
        "send_escalation_digest": "true",        # staff: vehicles in Technical Escalation
        "send_known_issues_checkin": "true",     # client: re-confirm vehicles marked Known Issue
        "send_tamper_report": "true",            # client: tampering risk report summary
    },
    "recovery": {
        # The "back online" FYI (notifications.send_recovery_notice), the
        # one email in this system triggered by a state change rather
        # than a schedule.
        #
        # requires_comment=true means only vehicles that someone has
        # actually commented on are worth an unsolicited email when they
        # reconnect. A fleet of 55 assets flapping in and out of
        # coverage generates a lot of reconnections that nobody asked a
        # question about, and mailing all of them is how a useful alert
        # becomes something the client filters away. A comment on file
        # is the client (or a technician) having said "this one
        # matters" - that is the control, and it is per asset without
        # needing a second list to maintain.
        "requires_comment": "true",
        # A vehicle whose reconnection already earns the two-button
        # reconnect-check email (an unanswered follow-up on file, see
        # notifications.check_reconnections) does not also need the
        # plain FYI about the same event in the same cycle.
        "suppress_when_reconnect_check_sent": "true",
    },
}


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_send_days(raw):
    """
    "monday" -> {0}; "monday,thursday" -> {0, 3}; "daily"/"" -> every
    day. An unrecognised name is dropped with a warning rather than
    silently becoming Monday - a typo'd day that quietly still sends on
    some other day is worse than one that's reported.
    """
    text = (raw or "").strip().lower()
    if not text or text in ("daily", "every day", "everyday", "*"):
        return set(range(7))
    out = set()
    for part in text.split(","):
        name = part.strip()
        if not name:
            continue
        if name in WEEKDAYS:
            out.add(WEEKDAYS.index(name))
        elif name[:3] in [d[:3] for d in WEEKDAYS]:
            out.add([d[:3] for d in WEEKDAYS].index(name[:3]))
        else:
            print(f"WARNING: settings.ini [digests] weekly_send_day has an unrecognised day {name!r} - ignoring it.")
    if not out:
        print("WARNING: settings.ini [digests] weekly_send_day resolved to no valid day; "
              "falling back to Monday so the digests still go out.")
        return {0}
    return out


def _parse_send_time(raw):
    """"08:00" -> (8, 0). Anything unparseable falls back to 08:00 with a
    warning, rather than raising and taking the whole import down over a
    cosmetic setting."""
    text = (raw or "").strip()
    try:
        hour_str, _, minute_str = text.partition(":")
        hour, minute = int(hour_str), int(minute_str or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(text)
        return hour, minute
    except (ValueError, TypeError):
        print(f"WARNING: settings.ini [digests] weekly_send_time={raw!r} is not a valid HH:MM - using 08:00.")
        return 8, 0


def load_settings(path="settings.ini"):
    config = configparser.ConfigParser()
    if os.path.exists(path):
        config.read(path)
    changed = False
    for section, values in DEFAULTS.items():
        if not config.has_section(section):
            config.add_section(section)
            changed = True
        for key, val in values.items():
            if not config.has_option(section, key):
                config.set(section, key, val)
                changed = True
    if changed or not os.path.exists(path):
        with open(path, "w") as f:
            config.write(f)

    return {
        "OFFLINE_THRESHOLD_DAYS": config.getfloat("thresholds", "offline_threshold_days"),
        "LONG_TERM_FAULT_DAYS": config.getint("thresholds", "long_term_fault_days"),
        "HIGH_PRIORITY_DAYS": config.getint("thresholds", "high_priority_days"),
        "BORDER_RADIUS_KM": config.getfloat("thresholds", "border_radius_km"),
        "IGNORE_DEMO_VEHICLES": config.getboolean("filters", "ignore_demo_vehicles"),
        "MIX_API_ORG_IDS": [o.strip() for o in config.get("mix_api", "org_ids").split(",") if o.strip()],
        "MIX_API_POLL_INTERVAL_MINUTES": config.getfloat("mix_api", "poll_interval_minutes"),
        "MIX_API_INTER_ORG_DELAY_SECONDS": config.getfloat("mix_api", "inter_org_delay_seconds"),
        "TELETRAC_API_CLIENT_IDS": [c.strip() for c in config.get("teletrac_api", "client_ids").split(",") if c.strip()],
        "TELETRAC_API_POLL_INTERVAL_MINUTES": config.getfloat("teletrac_api", "poll_interval_minutes"),
        "TELETRAC_API_INTER_CLIENT_DELAY_SECONDS": config.getfloat("teletrac_api", "inter_client_delay_seconds"),
        "FT_CLOUD_API_FLEET_IDS": [f.strip() for f in config.get("ft_cloud_api", "fleet_ids").split(",") if f.strip()],
        "FT_CLOUD_API_POLL_INTERVAL_MINUTES": config.getfloat("ft_cloud_api", "poll_interval_minutes"),
        "FT_CLOUD_API_INTER_CALL_DELAY_SECONDS": config.getfloat("ft_cloud_api", "inter_call_delay_seconds"),
        "FT_CLOUD_API_POSITION_SOURCE": config.get("ft_cloud_api", "position_source").strip().lower(),
        "FT_CLOUD_API_POSITION_LOOKBACK_DAYS": config.getint("ft_cloud_api", "position_lookback_days"),
        "FT_CLOUD_API_WEBHOOK_POSITION_MAX_AGE_MINUTES": config.getfloat(
            "ft_cloud_api", "webhook_position_max_age_minutes"),
        "PENDING_CONFIRMATION_OVERDUE_DAYS": config.getfloat("digests", "pending_confirmation_overdue_days"),
        "WEEKLY_SEND_DAYS": _parse_send_days(config.get("digests", "weekly_send_day")),
        "WEEKLY_SEND_TIME": _parse_send_time(config.get("digests", "weekly_send_time")),
        "WEEKLY_SEND_WINDOW_HOURS": config.getfloat("digests", "weekly_send_window_hours"),
        "SEND_PENDING_DIGEST": config.getboolean("digests", "send_pending_digest"),
        "SEND_ESCALATION_DIGEST": config.getboolean("digests", "send_escalation_digest"),
        "SEND_KNOWN_ISSUES_CHECKIN": config.getboolean("digests", "send_known_issues_checkin"),
        "SEND_TAMPER_REPORT": config.getboolean("digests", "send_tamper_report"),
        "RECOVERY_REQUIRES_COMMENT": config.getboolean("recovery", "requires_comment"),
        "RECOVERY_SUPPRESS_WHEN_RECONNECT_CHECK_SENT": config.getboolean(
            "recovery", "suppress_when_reconnect_check_sent"),
        # Raw strings too, so the Settings panel can show back exactly
        # what's in the file rather than a re-rendered guess at it.
        "WEEKLY_SEND_DAY_RAW": config.get("digests", "weekly_send_day").strip(),
        "WEEKLY_SEND_TIME_RAW": config.get("digests", "weekly_send_time").strip(),
        "_path": os.path.abspath(path),
    }
