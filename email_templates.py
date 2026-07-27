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

import html

PRIMARY = "#2E8CFF"
PRIMARY_2 = "#7B61FF"
SUCCESS = "#2EE6A6"
WARNING = "#FFB020"
INK = "#12131A"
MUTED = "#6B7280"
BORDER = "#E5E7EB"
BG = "#F4F5F7"

_esc = html.escape


def _shell(inner_html, preheader_note=""):
    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 0;">
  <tr><td align="center">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0"
      style="background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid {BORDER};font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
      <tr>
        <td style="background:linear-gradient(135deg,{PRIMARY},{PRIMARY_2});background-color:{PRIMARY};padding:22px 28px;">
          <span style="color:#ffffff;font-size:13px;font-weight:700;letter-spacing:0.4px;">GTL FLEET INTELLIGENCE</span>
        </td>
      </tr>
      {inner_html}
      <tr>
        <td style="padding:18px 28px;border-top:1px solid {BORDER};">
          <span style="color:{MUTED};font-size:11px;line-height:1.6;">
            Globe Trotters Ltd &middot; Cross-Platform Integrity Monitor &middot; telemetry by Teletrac Fleet Solutions.<br>
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


def build_update_email(plate, comment, author, role, timestamp, no_followup_url, needs_attention_url, recent_trail):
    """
    Sent to the client when a technician/admin adds a comment. Two large
    tap-target buttons, pre-selecting the answer they land on - nothing
    changes state until that page is submitted, so a mail scanner
    prefetching either link only loads a page, it can never act.
    """
    trail_rows = "".join(
        f'<tr><td style="padding:6px 0;border-top:1px solid {BORDER};">'
        f'<span style="font-size:12px;color:{INK};">{_esc(e["comment"])}</span><br>'
        f'<span style="font-size:10.5px;color:{MUTED};">{_esc(e["addedBy"])} &middot; {_esc(e["date"])}</span>'
        f'</td></tr>'
        for e in recent_trail[:3]
    )
    inner = f"""
      <tr><td style="padding:26px 28px 8px;">
        <span style="font-size:19px;font-weight:800;color:{INK};">{_esc(plate)}</span><br>
        <span style="font-size:12px;color:{MUTED};">Update from {_esc(author)} ({_esc(role)}) &middot; {_esc(timestamp)}</span>
      </td></tr>
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
      {"<tr><td style='padding:0 28px 22px;'><span style='font-size:10.5px;color:" + MUTED + ";text-transform:uppercase;letter-spacing:0.5px;'>Recent history</span><table role='presentation' width='100%' cellpadding='0' cellspacing='0'>" + trail_rows + "</table></td></tr>" if trail_rows else ""}
    """
    preheader = f"{comment[:110]}"
    return _shell(inner), preheader


def build_outcome_email(plate, resolved_comment, author, requires_followup, timestamp):
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
      <tr><td style="padding:0 28px 22px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:{BG};border-radius:10px;padding:16px;">
          <tr><td style="font-size:14px;color:{INK};line-height:1.6;">{_esc(resolved_comment)}</td></tr>
          <tr><td style="padding-top:8px;font-size:11px;color:{MUTED};">&mdash; {_esc(author)}, {_esc(timestamp)}</td></tr>
        </table>
      </td></tr>
    """
    preheader = headline
    return _shell(inner), preheader
