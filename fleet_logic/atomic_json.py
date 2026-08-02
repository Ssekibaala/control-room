"""
One safe way to write a JSON file that something else may be reading.

Three places need this - the API poll snapshots (app.py), the dashboard
payload (importer/run_import.py) and the client registry cache
(client_registry.py) - and all three are written by background threads
while HTTP requests read the same paths. Keeping one implementation
here rather than three copies also keeps the two hard-won details in
one place; both came from real failures, not caution:

  * write to a temp file then rename, never straight into the target.
    A plain open(path, "w") is visible to readers mid-write, so a
    concurrent reader intermittently sees truncated JSON. Observed as
    "snapshot is unreadable, skipping", silently dropping a whole poll.

  * give the temp file a name unique to this writer. With a shared
    "<path>.tmp", two threads writing at once fight over one temp file
    and the loser dies on Windows with "being used by another process".

fleet_logic is the lowest layer in this project, so app.py and
importer/ import from here - never the other way around.
"""

import os
import json
import time
import threading


def replace_with_retry(tmp_path, path, attempts=5, delay=0.15):
    """
    os.replace() is atomic on POSIX and Windows alike, but on Windows it
    also fails outright ("Access is denied" / "being used by another
    process") if anything merely has the destination open at that
    instant - another thread reading it, or a file-sync client watching
    the folder (this project lives in a synced folder during local dev).
    Retrying is safe: nothing is half-written, the temp file is still
    complete, so a few short waits turn a spurious hard failure into a
    brief pause.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp_path, path)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def write_json_atomic(path, data, indent=None):
    """Write data to path as JSON so no reader ever sees a partial file."""
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=indent, default=str)
        replace_with_retry(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)  # don't leave a half-written temp file behind
        except OSError:
            pass
        raise
