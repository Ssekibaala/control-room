"""
HTML email bodies. Deliberately plain table-based HTML with inline
styles only - the one layout method every mail client (including
Outlook's Word rendering engine) supports consistently. No :hover,
no external stylesheet, no flexbox/grid: none of those render
reliably in an inbox, whatever a browser preview might suggest.

Colours are the app's own tokens (see templates/dashboard.html's
:root), hardcoded here because email clients cannot read CSS custom
properties.
"""

import base64
import functools
import html
import os

PRIMARY = "#0E5C43"
PRIMARY_2 = "#1FAF7B"
SUCCESS = "#2EE6A6"
WARNING = "#FFB020"
INFO = "#4FC3FF"
DANGER = "#FF5C7A"
INK = "#12131A"
MUTED = "#6B7280"
BORDER = "#E5E7EB"
BG = "#F4F5F7"

_esc = html.escape

_LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "Teletrac_Fleet_Solutions_logo.png")
# History that can pile up over a vehicle's lifetime; capped so one very
# old, very chatty plate can't turn its email into a multi-megabyte page.
# Not a silent cap - the "+N earlier" line below says exactly what's cut.
MAX_HISTORY_ROWS = 25


def _is_probably_local_leftover(url):
    return any(marker in url.lower() for marker in ("localhost", "127.0.0.1", "0.0.0.0"))


def _public_base_url():
    """
    Same reasoning as app.py's public_base_url() and run_import.py's
    _public_base_url() - deliberately reimplemented rather than
    imported, since this module has no Flask app/request to fall back
    to and is called from both request and background-thread contexts.
    """
    configured = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if configured and _is_probably_local_leftover(configured):
        return ""
    return configured.rstrip("/")


@functools.lru_cache(maxsize=1)
def _embedded_logo_src():
    """
    The logo as a data: URI. This used to be the ONLY way the logo was
    sent, specifically to stop it depending on the live app being
    reachable at the moment a mail client renders it - a linked image
    was showing up as a broken-image icon while Render's free tier was
    asleep/cold-starting.

    It's now only a fallback for when no public URL is configured at
    all (e.g. local dev): a data: URI has a much worse problem than an
    occasional cold start - Gmail's iOS/Android apps refuse to render
    data: image sources entirely, every time, for security reasons.
    Gmail's own web/desktop client renders them fine, which is exactly
    why this showed up as "logo missing on phone, fine on PC" once a
    real recipient actually opened these on their phone.
    """
    try:
        with open(_LOGO_PATH, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


def _logo_src():
    """
    A real https:// URL when we have one to link to (renders correctly
    in every client, including Gmail's mobile apps) - the data: URI
    fallback only covers local dev / a misconfigured deploy with no
    PUBLIC_BASE_URL, where there's no reachable URL to link to at all.
    """
    base = _public_base_url()
    if base:
        return f"{base}/static/Teletrac_Fleet_Solutions_logo.png"
    return _embedded_logo_src()


DEFAULT_CLIENT_LABEL = "Fleet Intelligence"


def _shell(inner_html, client=None):
    """
    client names the organisation this email is ABOUT, and appears in
    the header badge and footer.

    It has to be a parameter rather than a constant: the shell said
    "GTL FLEET INTELLIGENCE" and "Globe Trotters Ltd" on every message,
    so once a second client existed, AGL and ADT were receiving mail
    about their own vehicles branded as another company's. Callers that
    genuinely aren't about one client fall back to a neutral label
    rather than naming whichever client happens to be first.
    """
    client_label = (client or "").strip() or DEFAULT_CLIENT_LABEL
    # The Teletrac mark sits on its own white plate inside the gradient
    # header, same reasoning as the dashboard sidebar: it's a third-party
    # brand with its own colours, not something to recolour to the app's
    # theme, and it needs real contrast to read at a glance in an inbox.
    logo_src = _logo_src()
    logo_html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="background:#ffffff;'
        f'border-radius:10px;padding:8px 14px;display:inline-block;"><tr><td>'
        f'<img src="{logo_src}" alt="Teletrac Fleet Solutions" height="34" '
        f'style="display:block;height:34px;width:auto;border:0;"></td></tr></table>'
        if logo_src else
        '<span style="color:#ffffff;font-size:13px;font-weight:700;letter-spacing:0.4px;">TELETRAC</span>'
    )
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 0;">
  <tr><td align="center">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0"
      style="background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {BORDER};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
      <tr>
        <td style="background:linear-gradient(135deg,{PRIMARY},{PRIMARY_2});background-color:{PRIMARY};padding:18px 28px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
            <td>{logo_html}</td>
            <td align="right">
              <span style="color:rgba(255,255,255,0.92);font-size:11.5px;font-weight:700;letter-spacing:0.5px;">
                {client_label.upper()}</span>
            </td>
          </tr></table>
        </td>
      </tr>
      {inner_html}
      <tr>
        <td style="padding:18px 28px;border-top:1px solid {BORDER};">
          <span style="color:{MUTED};font-size:11px;line-height:1.6;">
            {client_label} &middot; Cross-Platform Integrity Monitor &middot; telemetry by Teletrac Fleet Solutions.<br>
            This is an automated message about a specific vehicle. Reply to this email or use the buttons above to respond.
          </span>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
""".strip()


def _badge(text, color):
    return (f'<span style="display:inline-block;padding:3px 10px;border-radius:20px;'
            f'font-size:10.5px;font-weight:700;letter-spacing:0.3px;color:{color};'
            f'background:{color}22;">{_esc(text)}</span>')


def _button(label, url, color):
    return (f'<a href="{url}" target="_blank" '
            f'style="display:inline-block;padding:13px 22px;border-radius:10px;background:{color};'
            f'color:#ffffff;text-decoration:none;font-size:13.5px;font-weight:700;'
            f'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">{_esc(label)}</a>')


def _reopened_banner(reopened, previous_closed_at):
    """Called out explicitly rather than left for the reader to notice
    buried in the history below - a case that was already marked
    resolved coming back is a different situation than a routine
    update, and the email should say so up front."""
    if not reopened:
        return ""
    when = f" on {_esc(previous_closed_at)}" if previous_closed_at else ""
    return f"""
      <tr><td style="padding:0 28px 14px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{INFO}1F;border:1px solid {INFO}66;border-radius:10px;padding:12px 14px;">
          <tr><td style="font-size:12.5px;color:{INK};line-height:1.5;">
            <span style="font-size:14px;">&#128260;</span>&nbsp;<b>Reopened</b> &mdash; this vehicle was previously
            marked resolved{when}, and a new comment has just brought it back.
          </td></tr>
        </table>
      </td></tr>
    """


def _history_row(e):
    badge = ""
    if e.get("requiresFollowup") is False:
        badge = "&nbsp;" + _badge("No follow-up", SUCCESS)
    elif e.get("requiresFollowup") is True:
        badge = "&nbsp;" + _badge("Follow-up", WARNING)
    return (
        f'<tr><td style="padding:8px 0;border-top:1px solid {BORDER};">'
        f'<span style="font-size:12px;color:{INK};">{_esc(e["comment"])}</span>{badge}<br>'
        f'<span style="font-size:10.5px;color:{MUTED};">{_esc(e["addedBy"])} &middot; {_esc(e["date"])}</span>'
        f'</td></tr>'
    )


def _history_block(entries, title="Comment history"):
    """Every prior comment on this vehicle, not just the last couple -
    capped at MAX_HISTORY_ROWS with an explicit "+N earlier" line rather
    than silently trimming, so a long-lived plate's email stays a
    reasonable size without hiding that anything was cut."""
    if not entries:
        return ""
    shown = entries[:MAX_HISTORY_ROWS]
    overflow = len(entries) - len(shown)
    more = (
        f'<tr><td style="padding:8px 0;border-top:1px solid {BORDER};">'
        f'<span style="font-size:10.5px;color:{MUTED};">'
        f'+{overflow} earlier comment{"s" if overflow != 1 else ""} not shown here &mdash; see the full trail in the dashboard.'
        f'</span></td></tr>'
        if overflow > 0 else ""
    )
    return (
        f'<tr><td style="padding:0 28px 22px;">'
        f'<span style="font-size:10.5px;color:{MUTED};text-transform:uppercase;letter-spacing:0.5px;">{_esc(title)}</span>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'{"".join(_history_row(e) for e in shown)}{more}</table></td></tr>'
    )


def _fallback_duration(days):
    """days_silent, phrased for a reader, when the precise episode isn't
    known. It's a float (classifier.py rounds to one decimal), so
    "0.6 days" has to become "14 hours" rather than int()'ing to zero."""
    try:
        days = float(days or 0)
    except (TypeError, ValueError):
        return "an unknown period"
    if days <= 0:
        return "less than a day"
    if days < 1:
        hours = max(1, int(round(days * 24)))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    whole = int(days)
    return f"{whole} day{'s' if whole != 1 else ''}"


def _offline_episode_block(offline_since=None, offline_platforms=None, offline_for=None,
                           last_seen=None):
    """
    The three facts about an offline spell that just ended: when it
    started, which platform(s) stopped reporting, and how long it
    lasted.

    These emails used to say only "back online", or "offline for N
    days" with N rounded off a float and no platform named. With three
    platforms watching each vehicle, "it was offline" doesn't say
    whether one feed dropped or all of them did - which is the
    difference between a device fault and a vehicle nobody can see. All
    three values are already computed when the transition is detected
    (see run_import._day_over_day); this just shows them.

    Renders nothing at all when the episode isn't known - an empty box
    with "Unknown" in it is worse than no box.
    """
    rows = []
    if offline_for:
        rows.append(("Offline for", offline_for))
    if offline_since:
        rows.append(("Went offline", offline_since))
    if last_seen:
        rows.append(("Last reported position", last_seen))
    if offline_platforms:
        label = "Platform silent" if len(offline_platforms) == 1 else "Platforms silent"
        rows.append((label, ", ".join(offline_platforms)))
    if not rows:
        return ""
    cells = "".join(
        f'<tr>'
        f'<td style="padding:3px 0;font-size:11.5px;color:{MUTED};white-space:nowrap;">{_esc(k)}&nbsp;&nbsp;</td>'
        f'<td style="padding:3px 0;font-size:12.5px;color:{INK};font-weight:700;">{_esc(str(v))}</td>'
        f'</tr>'
        for k, v in rows
    )
    return (
        f'<tr><td style="padding:0 28px 14px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{INFO}14;border:1px solid {INFO}55;border-radius:10px;padding:12px 14px;">'
        f'<tr><td><table role="presentation" cellpadding="0" cellspacing="0">{cells}</table></td></tr>'
        f'</table></td></tr>'
    )


def build_reconnect_check_email(plate, days_offline, open_comment, timestamp,
                                no_followup_url, needs_attention_url, recent_trail, client=None,
                                offline_since=None, offline_platforms=None, offline_for=None):
    """
    Sent when a vehicle that was offline reports back online while it
    still has an unanswered follow-up request on file. Deliberately a
    QUESTION, not a closure: the vehicle reconnecting doesn't prove the
    reported problem is fixed (it might not even be about connectivity
    at all - see notifications.check_reconnections), so this reuses the
    exact same two-button respond flow a human update gets, rather than
    auto-closing anything on the system's own authority.
    """
    # offline_for when we have the real episode, days_silent as the
    # fallback. int(days_offline) on a float like 0.6 printed "0 days",
    # which reads as a glitch rather than "fourteen hours".
    duration = offline_for or _fallback_duration(days_offline)
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span>
        &nbsp;{_badge("Back online", INFO)}<br>
        <span style="font-size:12px;color:{MUTED};">Automatic check &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 14px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          This vehicle was offline for {_esc(duration)} and has just reported back in.
          It still has an open follow-up request on file:
        </span>
      </td></tr>
      {_offline_episode_block(offline_since, offline_platforms, offline_for)}
      <tr><td style="padding:0 28px 14px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:14px;color:{INK};line-height:1.6;">{_esc(open_comment)}</td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:4px 28px 22px;">
        <span style="font-size:11.5px;color:{MUTED};display:block;margin-bottom:12px;">
          Now that it's back online, does this resolve it?
        </span>
        {_button("No follow-up needed", no_followup_url, SUCCESS)}
        &nbsp;&nbsp;
        {_button("Needs attention", needs_attention_url, WARNING)}
      </td></tr>
      {_history_block(recent_trail)}
    """
    preheader = f"{_esc(plate)} is back online - does this resolve the open follow-up?"
    return _shell(inner, client), preheader


def build_update_email(plate, comment, author, role, timestamp, no_followup_url, needs_attention_url,
                       recent_trail, reopened=False, previous_closed_at=None, client=None):
    """
    Sent to the client when a technician/admin adds a comment. Two large
    tap-target buttons, pre-selecting the answer they land on - nothing
    changes state until that page is submitted, so a mail scanner
    prefetching either link only loads a page, it can never act.
    """
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span><br>
        <span style="font-size:12px;color:{MUTED};">Update from {_esc(author)} ({_esc(role)}) &middot; {_esc(timestamp)}</span>
      </td></tr>
      {_reopened_banner(reopened, previous_closed_at)}
      <tr><td style="padding:14px 28px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:14px;color:{INK};line-height:1.6;">{_esc(comment)}</td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:4px 28px 22px;">
        <span style="font-size:11.5px;color:{MUTED};display:block;margin-bottom:12px;">
          Does this vehicle need follow-up from our side?
        </span>
        {_button("No follow-up needed", no_followup_url, SUCCESS)}
        &nbsp;&nbsp;
        {_button("Needs attention", needs_attention_url, WARNING)}
      </td></tr>
      {_history_block(recent_trail)}
    """
    # Escaped: mailer.py splices the preheader into the HTML body
    # verbatim, so an unescaped comment containing "<" (a plate written
    # as "<unknown>", a note about a "<2 day" outage) silently ate the
    # rest of the preview text or broke the markup after it. Every other
    # template here already escapes; this one interpolated the raw
    # comment.
    preheader = _esc(comment[:110])
    return _shell(inner, client), preheader


def build_outcome_email(plate, resolved_comment, author, requires_followup, timestamp,
                        recent_trail=None, reopened=False, previous_closed_at=None, client=None):
    """Sent to everyone in the thread once the client (or anyone) records
    an answer - states the real outcome, never a blanket 'closed' when
    follow-up was actually requested."""
    if requires_followup is False:
        badge, headline = _badge("No follow-up needed", SUCCESS), "Marked as a known issue, no action required."
    else:
        badge, headline = _badge("Follow-up requested", WARNING), "Flagged for technician follow-up."
    inner = f"""
      <tr><td style="padding:26px 28px 6px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span>
        &nbsp;{badge}
      </td></tr>
      <tr><td style="padding:0 28px 14px;">
        <span style="font-size:13px;color:{MUTED};">{_esc(headline)}</span>
      </td></tr>
      {_reopened_banner(reopened, previous_closed_at)}
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:14px;color:{INK};line-height:1.6;">{_esc(resolved_comment)}</td></tr>
          <tr><td style="padding-top:8px;font-size:11px;color:{MUTED};">&mdash; {_esc(author)}, {_esc(timestamp)}</td></tr>
        </table>
      </td></tr>
      {_history_block(recent_trail or [])}
    """
    preheader = headline
    return _shell(inner, client), preheader


def _pending_digest_row(v):
    """One vehicle's block inside the weekly client digest - its own
    respond buttons, so answering doesn't require opening anything else.
    `overdue` (from settings.PENDING_CONFIRMATION_OVERDUE_DAYS) is purely
    a visual flag here - escalation itself stays days-based regardless
    of whether this email was ever opened or answered."""
    overdue = f"&nbsp;{_badge('Overdue', WARNING)}" if v.get("overdue") else ""
    return f"""
      <tr><td style="padding:16px 0;border-top:1px solid {BORDER};">
        <span style="font-size:15px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>{overdue}<br>
        <span style="font-size:11.5px;color:{MUTED};">{_esc(v['detail'])}</span>
        <div style="margin-top:10px;">
          {_button("No follow-up needed", v['no_url'], SUCCESS)}
          &nbsp;&nbsp;
          {_button("Needs attention", v['need_url'], WARNING)}
        </div>
      </td></tr>
    """


def _critical_no_feedback_row(v):
    """A vehicle in Technical Escalation with zero feedback on file -
    nobody has told the client about it yet, which is a different (and
    more urgent) situation than one they've already been asked about
    and simply haven't answered. Same respond buttons as the pending
    section: clicking either one creates that vehicle's FIRST feedback
    entry, exactly like answering any other respond-token email."""
    return f"""
      <tr><td style="padding:16px 0;border-top:1px solid {BORDER};">
        <span style="font-size:15px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>
        &nbsp;{_badge('No feedback on file', DANGER)}<br>
        <span style="font-size:11.5px;color:{MUTED};">{_esc(v['detail'])}</span>
        <div style="margin-top:10px;">
          {_button("No follow-up needed", v['no_url'], SUCCESS)}
          &nbsp;&nbsp;
          {_button("Needs attention", v['need_url'], WARNING)}
        </div>
      </td></tr>
    """


def build_pending_confirmation_digest_email(vehicles, timestamp, critical_no_feedback=None, client=None):
    """
    One weekly email to the client listing EVERY vehicle currently in
    Pending Customer Confirmation (some, not all, platforms silent) -
    not a per-vehicle transition alert. Each vehicle carries its own
    signed respond links, so answering any of them works exactly like
    the single-vehicle update email always has; nothing new for
    /feedback/respond to handle. `vehicles` is a list of dicts:
    {plate, detail, no_url, need_url, overdue}.

    `critical_no_feedback` (same dict shape, minus `overdue`) is a
    second, separately-labelled section: vehicles already in Technical
    Escalation that have never had a single feedback entry - actively
    escalated, but the client has never actually been told. Nothing
    upstream proactively emails about these otherwise (staff-initiated
    comments and the reconnect-check both require an entry to already
    exist), so without this section they could sit silently forever.
    """
    critical_no_feedback = critical_no_feedback or []
    rows = "".join(_pending_digest_row(v) for v in vehicles)
    critical_rows = "".join(_critical_no_feedback_row(v) for v in critical_no_feedback)
    total = len(vehicles) + len(critical_no_feedback)
    critical_section = f"""
      <tr><td style="padding:8px 28px 4px;">
        <span style="font-size:13px;font-weight:800;color:{DANGER};">Critical &mdash; no feedback on file yet</span><br>
        <span style="font-size:12px;color:{MUTED};">
          Every platform has gone silent on these and we haven't heard from you about them at all.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{critical_rows}</table>
      </td></tr>
    """ if critical_rows else ""
    pending_section = f"""
      <tr><td style="padding:0 28px 4px;">
        {"<span style='font-size:13px;font-weight:800;color:" + INK + ";'>Awaiting your confirmation</span><br>" if critical_rows else ""}
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          These vehicles have one or more tracking platforms not reporting. For each one below, let us know
          whether it needs follow-up from our side.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
    """ if rows else ""
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Vehicles needing your input</span><br>
        <span style="font-size:12px;color:{MUTED};">{total} vehicle(s) &middot; {_esc(timestamp)}</span>
      </td></tr>
      {critical_section}
      {pending_section}
    """
    preheader = f"{total} vehicle(s) need your input"
    return _shell(inner, client), preheader


def _known_issue_checkin_row(v):
    """Re-confirmation row: shows what was said last time (comment,
    author, date) so the client isn't asked blind - they see exactly
    what they're being asked to reconfirm still holds."""
    return f"""
      <tr><td style="padding:16px 0;border-top:1px solid {BORDER};">
        <span style="font-size:15px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>
        &nbsp;{_badge('Known issue', SUCCESS)}<br>
        <span style="font-size:12.5px;color:{INK};display:block;margin:6px 0;">&ldquo;{_esc(v['last_comment'])}&rdquo;</span>
        <span style="font-size:10.5px;color:{MUTED};">&mdash; {_esc(v['last_author'])}, {_esc(v['last_date'])}</span>
        <div style="margin-top:10px;">
          {_button("Still the same", v['no_url'], SUCCESS)}
          &nbsp;&nbsp;
          {_button("Actually needs attention", v['need_url'], WARNING)}
        </div>
      </td></tr>
    """


def build_known_issues_checkin_digest_email(vehicles, timestamp, client=None):
    """
    Weekly email to the client re-confirming every vehicle currently
    marked Known Issue (an earlier "no follow-up needed"). A known
    issue can stop being accurate - the underlying problem could
    recur, or what was true a month ago just isn't anymore - so this
    keeps asking rather than treating one old "no follow-up" as
    permanent. Same snapshot-not-transition shape as the other two
    digests: reappears every interval for as long as the vehicle stays
    a Known Issue, not just once. `vehicles` is a list of dicts:
    {plate, last_comment, last_author, last_date, no_url, need_url}.
    """
    rows = "".join(_known_issue_checkin_row(v) for v in vehicles)
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Quick check-in on known issues</span><br>
        <span style="font-size:12px;color:{MUTED};">{len(vehicles)} vehicle(s) &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 4px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          These were previously marked as known issues needing no follow-up. Please confirm each one is
          still accurate.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
    """
    preheader = f"Please reconfirm {len(vehicles)} known issue(s)"
    return _shell(inner, client), preheader


def _escalation_digest_row(v):
    return f"""
      <tr><td style="padding:14px 0;border-top:1px solid {BORDER};">
        <span style="font-size:15px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>
        &nbsp;{_badge(v['severity'], DANGER)}<br>
        <span style="font-size:11.5px;color:{MUTED};">{_esc(v['detail'])}</span>
        {f'<div style="margin-top:8px;"><a href="{v["dashboard_url"]}" target="_blank" style="font-size:12px;color:{PRIMARY};font-weight:700;text-decoration:none;">Open in dashboard &rarr;</a></div>' if v.get('dashboard_url') else ''}
      </td></tr>
    """


def build_technical_escalation_digest_email(vehicles, timestamp, client=None):
    """
    One weekly email to staff (admin/technician) listing every vehicle
    currently in Technical Escalation (all platforms silent past the
    offline threshold). Purely informational - staff already have full
    dashboard access, so this links back to each vehicle rather than
    offering a response-token flow like the client digest does.
    `vehicles` is a list of dicts: {plate, detail, severity, dashboard_url}.
    """
    rows = "".join(_escalation_digest_row(v) for v in vehicles)
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Vehicles in Technical Escalation</span><br>
        <span style="font-size:12px;color:{MUTED};">{len(vehicles)} vehicle(s) &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 4px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          Every tracking platform has gone silent on these vehicles past the offline threshold. Needs field
          action.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
    """
    preheader = f"{len(vehicles)} vehicle(s) in Technical Escalation"
    return _shell(inner, client), preheader


def _severity_band_row(b):
    color = DANGER if b["severity"] == "Critical" else (WARNING if b["severity"] == "High" else INFO)
    return f"""
      <tr>
        <td style="padding:8px 0;border-top:1px solid {BORDER};font-size:12.5px;color:{INK};font-weight:700;">
          {_badge(b['severity'], color)}&nbsp;<span style="color:{MUTED};font-weight:400;">{_esc(b['rule'])}</span>
        </td>
        <td style="padding:8px 0;border-top:1px solid {BORDER};text-align:right;font-size:12.5px;color:{INK};">
          {b['confirmed']} confirmed &middot; {b['unconfirmed']} unconfirmed
        </td>
      </tr>
    """


def _top_vehicle_row(v):
    return f"""
      <tr><td style="padding:10px 0;border-top:1px solid {BORDER};">
        <span style="font-size:13.5px;font-weight:800;color:{INK};">{_esc(v['vehicle'])}</span>
        <span style="font-size:11px;color:{MUTED};">&nbsp;({_esc(str(v['fleetNumber']))})</span><br>
        <span style="font-size:11.5px;color:{MUTED};">
          {v['confirmedCases']} confirmed gap(s) &middot; worst {v['worstGapKm']:.1f} km unexplained relocation
        </span>
      </td></tr>
    """


def _checked_asset_row(a):
    return f"""
      <tr><td style="padding:10px 0;border-top:1px solid {BORDER};">
        <span style="font-size:13px;font-weight:800;color:{INK};">{_esc(a['plate'])}</span>
        &nbsp;{_badge('Reviewed', SUCCESS)}<br>
        <span style="font-size:12px;color:{INK};display:block;margin:4px 0;">&ldquo;{_esc(a['comment'])}&rdquo;</span>
        <span style="font-size:10.5px;color:{MUTED};">&mdash; {_esc(a['checkedBy'])}, {_esc(a['checkedAt'])}</span>
      </td></tr>
    """


def build_tamper_risk_report_email(summary, severity_bands, top_vehicles, checked_assets, timestamp, client=None):
    """
    Weekly email to the client summarising the tampering/location-
    integrity analysis (importer/tamper_engine.py): unexplained
    relocations with no logged trip, cross-referenced against power
    disconnect/reconnect events. `confirmed` means a disconnect was
    logged inside the gap window - the strongest signal something was
    deliberately powered down; `unconfirmed` means the location gap
    exists but no power event explains it, worth a look but weaker.

    top_vehicles are the worst offenders by furthest unexplained
    relocation distance - the ones this email actually asks to have
    made available for a physical check, not every single gap.

    checked_assets (sheets_store.load_tamper_checks(), flattened) are
    shown in their own section, explicitly labelled as already
    reviewed - these are NOT part of confirmed/unconfirmed above
    (run_import.py's _filter_checked_gaps excludes any gap dated on or
    before its check), so without this section a client who'd already
    had a vehicle checked would see it simply vanish with no
    explanation of why.
    """
    band_rows = "".join(_severity_band_row(b) for b in severity_bands)
    top_rows = "".join(_top_vehicle_row(v) for v in top_vehicles[:8])
    checked_rows = "".join(_checked_asset_row(a) for a in checked_assets[:10])

    ask_section = f"""
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:13.5px;color:{INK};line-height:1.6;">
            <b>Please make the vehicle(s) below available for a physical check</b> so we can confirm what
            actually happened during these gaps - the strongest lead we have on each is listed underneath it.
          </td></tr>
          <tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{top_rows}</table></td></tr>
        </table>
      </td></tr>
    """ if top_rows else ""

    checked_section = f"""
      <tr><td style="padding:8px 28px 4px;">
        <span style="font-size:13px;font-weight:800;color:{SUCCESS};">Already reviewed</span><br>
        <span style="font-size:12px;color:{MUTED};">
          These were physically checked already - excluded from the confirmed/unconfirmed counts above.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{checked_rows}</table>
      </td></tr>
    """ if checked_rows else ""

    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Tampering &amp; location-integrity report</span><br>
        <span style="font-size:12px;color:{MUTED};">{_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 14px;">
        <span style="font-size:13px;color:{INK};line-height:1.6;">
          {summary.get('gaps_checked', 0)} location gap(s) were flagged on your vehicles this week.
          <b>{summary.get('confirmed', 0)} are confirmed</b> (a power disconnect was logged during the gap) and
          <b>{summary.get('unconfirmed', 0)} are unconfirmed</b> (the gap exists, no power event explains it).
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 4px;">
        <span style="font-size:13px;font-weight:800;color:{INK};">By severity</span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{band_rows}</table>
      </td></tr>
      {ask_section}
      {checked_section}
    """
    preheader = f"{summary.get('confirmed', 0)} confirmed, {summary.get('unconfirmed', 0)} unconfirmed this week"
    return _shell(inner, client), preheader


def _recovery_row(v):
    """
    One recovered vehicle. The headline is the outage that just ended,
    not the reconnection: "back online" on its own tells the reader
    nothing they can act on, while "offline 4 days on MiX Unity, last
    seen near Athi River" tells them whether to care.
    """
    def line(text, strong=False):
        color, weight = (INK, "700") if strong else (MUTED, "400")
        return (f'<span style="font-size:11.5px;color:{color};font-weight:{weight};'
                f'display:block;margin-top:2px;">{text}</span>')

    details = ""
    if v.get("offline_for"):
        platforms = v.get("offline_platforms") or []
        where = f" on {_esc(', '.join(platforms))}" if platforms else ""
        details += line(f'Was offline for {_esc(v["offline_for"])}{where}', strong=True)
    if v.get("offline_since"):
        details += line(f'Went offline {_esc(v["offline_since"])}')
    if v.get("last_seen"):
        details += line(f'Last position before the outage: {_esc(v["last_seen"])}')
    details += line(f'Last known location: {_esc(v["location"])}')
    if v.get("comment"):
        details += line(f'Comment on file: &ldquo;{_esc(v["comment"])}&rdquo;')

    return f"""
      <tr><td style="padding:12px 0;border-top:1px solid {BORDER};">
        <span style="font-size:13.5px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>
        &nbsp;{_badge('Back online', SUCCESS)}
        {details}
      </td></tr>
    """


def build_recovery_notice_email(vehicles, timestamp, client=None):
    """
    Plain FYI, no response needed: vehicles that came back online this
    import cycle. Deliberately separate from check_reconnections'
    two-button ask - that email fires for the subset with an unanswered
    "needs follow-up" comment on file, asking whether reconnecting
    resolves it, and a vehicle that gets it is excluded from this one so
    a single reconnection never produces two emails
    (notifications.send_recovery_notice's skip_plates).

    Which vehicles reach here is settings.ini [recovery]'s decision, not
    this template's - by default only ones somebody has commented on
    (requires_comment), because a fleet reconnecting all day long turns
    an unfiltered version of this email into something the client
    learns to ignore.
    """
    rows = "".join(_recovery_row(v) for v in vehicles)
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Back online</span><br>
        <span style="font-size:12px;color:{MUTED};">{len(vehicles)} vehicle(s) &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 4px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          These vehicles were offline and have reconnected since the last check. Each one shows how long it
          was silent and which platform stopped reporting. No action needed - this is purely informational.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
    """
    preheader = f"{len(vehicles)} vehicle(s) reconnected"
    return _shell(inner, client), preheader


def build_internal_action_email(plate, comment, author, role, timestamp, recent_trail,
                                client=None, dashboard_url=""):
    """
    A Recommended Action - the technician's operational instruction to
    the field - going to STAFF ONLY.

    This template exists because there wasn't one: notifications.
    on_comment_added took an entry_type argument and never read it, so
    an action note went out on the client-facing update template,
    complete with "Does this vehicle need follow-up from our side?" and
    two respond buttons. app.py refuses to let a client submit one of
    these and permissions.py strips them from every client payload; the
    email was the one place the distinction wasn't honoured.

    No respond buttons, deliberately: an internal instruction isn't a
    question anybody is being asked to answer, and staff act from the
    dashboard, which this links to instead.
    """
    link = (f'<tr><td style="padding:0 28px 22px;">'
            f'<a href="{dashboard_url}" target="_blank" style="font-size:13px;color:{PRIMARY};'
            f'font-weight:700;text-decoration:none;">Open {_esc(plate)} in the dashboard &rarr;</a>'
            f'</td></tr>') if dashboard_url else ""
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span>
        &nbsp;{_badge("Internal - recommended action", INFO)}<br>
        <span style="font-size:12px;color:{MUTED};">
          Set by {_esc(author)} ({_esc(role)}) &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 14px;">
        <span style="font-size:12.5px;color:{MUTED};line-height:1.5;">
          Internal note for the team - this has not been sent to the customer.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 18px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:14px;color:{INK};line-height:1.6;">{_esc(comment)}</td></tr>
        </table>
      </td></tr>
      {link}
      {_history_block(recent_trail or [])}
    """
    preheader = _esc(f"Internal action for {plate}: {comment[:90]}")
    return _shell(inner, client), preheader


def _platform_seen_row(p):
    online = p["status"] == "Online"
    return f"""
      <tr>
        <td style="padding:7px 0;border-top:1px solid {BORDER};font-size:12.5px;color:{INK};">
          {_esc(p['platform'])}&nbsp;{_badge(p['status'], SUCCESS if online else DANGER)}
        </td>
        <td style="padding:7px 0;border-top:1px solid {BORDER};text-align:right;font-size:12px;color:{MUTED};">
          {_esc(p['seen'])}
        </td>
      </tr>
    """


def build_asset_summary_email(plate, info, history, timestamp, client=None, note=None,
                              sent_by="", dashboard_url="", offline_detail="",
                              platform_seen=None):
    """
    One vehicle, everything currently known about it, for the ad-hoc
    "send this asset's status to a named person" path
    (notifications.send_manual_report).

    Carries the per-platform last-seen table rather than a single
    status word. With three platforms watching one vehicle, "Offline"
    alone doesn't distinguish one dead feed from a vehicle nobody can
    see at all, and that distinction is the entire reason this app
    reads three platforms instead of one.

    No respond buttons - see _send_asset_summary's docstring: the
    recipient was typed into a box, not drawn from the client registry,
    and those buttons are a write credential.
    """
    status = info.get("status") or "Unknown"
    status_color = {
        "Online": SUCCESS, "Known Issue": INFO,
        "Pending Customer Confirmation": WARNING, "Technical Escalation": DANGER,
    }.get(status, MUTED)

    note_block = f"""
      <tr><td style="padding:0 28px 18px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{WARNING}1A;border:1px solid {WARNING}66;border-radius:10px;padding:14px;">
          <tr><td style="font-size:13px;color:{INK};line-height:1.6;">{_esc(note)}</td></tr>
          <tr><td style="padding-top:6px;font-size:11px;color:{MUTED};">
            &mdash; {_esc(sent_by or "sent from the control room")}</td></tr>
        </table>
      </td></tr>
    """ if note else ""

    seen_rows = "".join(_platform_seen_row(p) for p in (platform_seen or []))
    seen_block = f"""
      <tr><td style="padding:0 28px 6px;">
        <span style="font-size:10.5px;color:{MUTED};text-transform:uppercase;letter-spacing:0.5px;">
          Last reported, per platform</span>
      </td></tr>
      <tr><td style="padding:0 28px 20px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{seen_rows}</table>
      </td></tr>
    """ if seen_rows else ""

    facts = [
        ("Status", status),
        ("Severity", info.get("severity") or "-"),
        ("Days silent", str(info.get("days_silent") if info.get("days_silent") is not None else "-")),
        ("Last known location", info.get("last_location") or "Unknown"),
    ]
    if offline_detail:
        facts.append(("Connectivity", offline_detail))
    fact_rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 0;font-size:11.5px;color:{MUTED};white-space:nowrap;">{_esc(k)}&nbsp;&nbsp;</td>'
        f'<td style="padding:4px 0;font-size:12.5px;color:{INK};font-weight:700;">{_esc(v)}</td>'
        f'</tr>'
        for k, v in facts
    )

    link = (f'<tr><td style="padding:0 28px 22px;">'
            f'<a href="{dashboard_url}" target="_blank" style="font-size:13px;color:{PRIMARY};'
            f'font-weight:700;text-decoration:none;">Open in the dashboard &rarr;</a></td></tr>'
            ) if dashboard_url else ""

    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span>
        &nbsp;{_badge(status, status_color)}<br>
        <span style="font-size:12px;color:{MUTED};">Vehicle status summary &middot; {_esc(timestamp)}</span>
      </td></tr>
      {note_block}
      <tr><td style="padding:0 28px 20px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td><table role="presentation" cellpadding="0" cellspacing="0">{fact_rows}</table></td></tr>
        </table>
      </td></tr>
      {seen_block}
      {link}
      {_history_block(history or [], title="Full comment history")}
    """
    preheader = _esc(f"{plate} - {status}. {offline_detail}".strip())
    return _shell(inner, client), preheader
