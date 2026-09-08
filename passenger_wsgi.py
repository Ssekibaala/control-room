"""
WSGI entrypoint for cPanel's "Setup Python App" (Passenger/mod_wsgi). Not
used by the Docker/Northflank deployment, which runs gunicorn directly
against app:app instead (see Dockerfile) - this file only matters on
cPanel-style hosting.

Namecheap's Python App feature (and Passenger generally) imports this
module and looks for a WSGI callable named `application` - app.py exposes
its Flask instance as `app`, so this is a plain alias, not a real
wrapper. See app.py's own `if __name__ == "__main__":` guard: Passenger
imports this module rather than running it, so that block (the Flask dev
server, debug=True) never executes under this entrypoint.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as application  # noqa: F401 - the name Passenger looks for
