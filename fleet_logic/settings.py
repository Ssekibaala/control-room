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
        "_path": os.path.abspath(path),
    }
