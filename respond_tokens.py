"""
Signs and verifies the plate+action pairs carried by an email's two
response buttons ("No follow-up needed" / "Needs attention"). Split out
of app.py so anything that needs to build one of these links doesn't
have to import the Flask app object - notably run_import.py, which
sends the reconnect-check email (see notifications.check_reconnections)
from outside any Flask request or app context, sometimes as a
standalone script with no running server at all.

Deliberately its own salt, separate from the session cookie, so a link
token and a session cookie can never be confused for each other even
though both derive from the same app secret.
"""

import os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

RESPOND_TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 days - long enough that an
# aging notification is still answerable, short enough that a years-old
# forwarded email eventually stops working.


def _serializer():
    secret = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
    return URLSafeTimedSerializer(secret, salt="feedback-respond-v1")


def make_respond_token(plate, action):
    return _serializer().dumps({"plate": plate, "action": action})


def read_respond_token(token):
    """Returns (payload, error_message). error_message is None on success,
    a human-readable reason otherwise - shown directly on the confirm page
    rather than a raw exception."""
    try:
        return _serializer().loads(token, max_age=RESPOND_TOKEN_MAX_AGE), None
    except SignatureExpired:
        return None, "This link has expired. Please ask for a fresh update, or reply to the email directly."
    except BadSignature:
        return None, "This link isn't valid. Please use the button from the original email."
