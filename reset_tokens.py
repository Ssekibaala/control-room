"""
Signs and verifies the token carried by a "forgot password" email link.

Split out the same way respond_tokens.py is: its own salt, so a reset
link can never be confused for a session cookie or a feedback-respond
link even though all three derive from the same app secret, and its
own (short) expiry, since an unclaimed password reset sitting in an
inbox for weeks is a real risk in a way an unclaimed feedback link
isn't.
"""

import os
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

RESET_TOKEN_MAX_AGE = 60 * 60  # 1 hour - long enough to get to the inbox
# and click through, short enough that a stale, forgotten reset email
# found months later can't still be used to take over the account.


def _serializer():
    secret = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
    return URLSafeTimedSerializer(secret, salt="password-reset-v1")


def make_reset_token(username):
    return _serializer().dumps({"username": username})


def read_reset_token(token):
    """Returns (username, error_message). error_message is None on
    success, a human-readable reason otherwise - shown directly on the
    reset-password page rather than a raw exception."""
    try:
        payload = _serializer().loads(token, max_age=RESET_TOKEN_MAX_AGE)
        return payload.get("username"), None
    except SignatureExpired:
        return None, "This reset link has expired. Request a new one below."
    except BadSignature:
        return None, "This reset link isn't valid. Request a new one below."
