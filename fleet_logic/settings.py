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
        # message listing every currently-pending/escalated vehicle,
        # sent no more often than this many days apart. Escalation
        # itself stays purely days-based (see classifier.py); these are
        # an additive notification layer, not a gate on anything.
        "pending_digest_interval_days": "7",     # client digest: vehicles in Pending Customer Confirmation
        "escalation_digest_interval_days": "7",  # staff digest: vehicles in Technical Escalation
        "pending_confirmation_overdue_days": "2",  # visual "overdue" flag inside the client digest only
        "known_issues_checkin_interval_days": "7",  # client digest: re-confirm vehicles marked Known Issue
        "tamper_report_interval_days": "7",      # client digest: tampering risk report summary
    },
}


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
        "PENDING_DIGEST_INTERVAL_DAYS": config.getfloat("digests", "pending_digest_interval_days"),
        "ESCALATION_DIGEST_INTERVAL_DAYS": config.getfloat("digests", "escalation_digest_interval_days"),
        "PENDING_CONFIRMATION_OVERDUE_DAYS": config.getfloat("digests", "pending_confirmation_overdue_days"),
        "KNOWN_ISSUES_CHECKIN_INTERVAL_DAYS": config.getfloat("digests", "known_issues_checkin_interval_days"),
        "TAMPER_REPORT_INTERVAL_DAYS": config.getfloat("digests", "tamper_report_interval_days"),
        "_path": os.path.abspath(path),
    }
