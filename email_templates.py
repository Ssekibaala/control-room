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


@functools.lru_cache(maxsize=1)
def _embedded_logo_src():
    """
    The logo as a data: URI, embedded directly in the email rather than
    linked to /static/... on the live app. A linked image depends on the
    app being reachable *at the moment the mail client renders it* - on
    a phone, that's whenever the recipient happens to open the message,
    possibly while the Render service is asleep/cold-starting, which is
    exactly what was showing up as a broken-image icon. A data URI has
    no such dependency: the bytes travel inside the email itself.
    """
    try:
        with open(_LOGO_PATH, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return None


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
    logo_src = _embedded_logo_src()
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


def build_reconnect_check_email(plate, days_offline, open_comment, timestamp,
                                no_followup_url, needs_attention_url, recent_trail, client=None):
    """
    Sent when a vehicle that was offline reports back online while it
    still has an unanswered follow-up request on file. Deliberately a
    QUESTION, not a closure: the vehicle reconnecting doesn't prove the
    reported problem is fixed (it might not even be about connectivity
    at all - see notifications.check_reconnections), so this reuses the
    exact same two-button respond flow a human update gets, rather than
    auto-closing anything on the system's own authority.
    """
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span>
        &nbsp;{_badge("Back online", INFO)}<br>
        <span style="font-size:12px;color:{MUTED};">Automatic check &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 14px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          This vehicle was offline for {int(days_offline)} day{"s" if days_offline != 1 else ""} and has just
          reported back in. It still has an open follow-up request on file:
        </span>
      </td></tr>
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
    preheader = f"{comment[:110]}"
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
          Out of {summary.get('gaps_checked', 0)} location gaps checked this week,
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
    comment_html = (
        f'<span style="font-size:11.5px;color:{MUTED};display:block;margin-top:2px;">'
        f'Last reported: &ldquo;{_esc(v["comment"])}&rdquo;</span>'
        if v.get("comment") else ""
    )
    return f"""
      <tr><td style="padding:10px 0;border-top:1px solid {BORDER};">
        <span style="font-size:13.5px;font-weight:800;color:{INK};">{_esc(v['plate'])}</span>
        &nbsp;{_badge('Back online', SUCCESS)}<br>
        <span style="font-size:11.5px;color:{MUTED};">Last known location: {_esc(v['location'])}</span>
        {comment_html}
      </td></tr>
    """


def build_recovery_notice_email(vehicles, timestamp, client=None):
    """
    Plain FYI, no response needed: every vehicle that came back online
    this import cycle. Deliberately separate from check_reconnections'
    two-button ask - that email only fires for the subset with an
    unanswered "needs follow-up" comment on file, asking whether
    reconnecting resolves it. This one is unconditional: without it, a
    vehicle recovering with no outstanding question on file reconnected
    completely silently, visible only as a quiet dashboard entry.
    """
    rows = "".join(_recovery_row(v) for v in vehicles)
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">Back online</span><br>
        <span style="font-size:12px;color:{MUTED};">{len(vehicles)} vehicle(s) &middot; {_esc(timestamp)}</span>
      </td></tr>
      <tr><td style="padding:0 28px 4px;">
        <span style="font-size:13px;color:{INK};line-height:1.5;">
          These vehicles were offline and have reconnected since the last check. No action needed - this is
          purely informational.
        </span>
      </td></tr>
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
    """
    preheader = f"{len(vehicles)} vehicle(s) reconnected"
    return _shell(inner, client), preheader
