"""Branded HTML email for brn dynamics.

Built for inboxes, not browsers: table layout, inline styles, no images (images and heavy
layouts push cold email into spam), web font with safe fallbacks, plain-text twin always sent.
"""
import html
import re

from . import db

INK = "#0B0C10"
PAPER = "#F4F4F0"
BLUE = "#010ECF"
BLUE_LIGHT = "#6E7BFF"
MUTED = "#6B6D76"


def _paragraphs(body):
    out = []
    for para in re.split(r"\n\s*\n", body.strip()):
        safe = html.escape(para.strip()).replace("\n", "<br>")
        safe = re.sub(r"(https?://[^\s<]+)", rf'<a href="\1" style="color:{BLUE};text-decoration:underline">\1</a>', safe)
        out.append(f'<p style="margin:0 0 16px 0">{safe}</p>')
    return "\n".join(out)


def render(subject, body, cta_url=None, cta_label=None, product_name=None):
    sender_name = db.setting("sender_name") or "brn dynamics"
    sender_email = db.setting("sender_email") or ""
    address = db.setting("company_address") or "brn dynamics"
    preheader = html.escape(re.sub(r"\s+", " ", body.strip().split("\n\n")[1] if "\n\n" in body.strip() else body)[:140])
    font = "'Space Grotesk','Helvetica Neue',Helvetica,Arial,sans-serif"
    body_font = "'DM Sans','Helvetica Neue',Helvetica,Arial,sans-serif"
    pill = (f'<span style="display:inline-block;padding:4px 10px;border-radius:99px;background:#1B1D27;color:{BLUE_LIGHT};'
            f'font:600 11px {body_font};letter-spacing:.04em;text-transform:uppercase">{html.escape(product_name)}</span>') if product_name else ""
    button = ""
    if cta_url:
        label = html.escape(cta_label or "Book a 15-min call")
        button = f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 24px 0"><tr>
<td style="border-radius:10px;background:{BLUE}">
<a href="{html.escape(cta_url)}" style="display:inline-block;padding:14px 26px;font:600 15px {font};color:#ffffff;text-decoration:none;border-radius:10px">{label} &rarr;</a>
</td></tr></table>"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{html.escape(subject)}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=DM+Sans:wght@400;600&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:{PAPER}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}">
<tr><td align="center" style="padding:28px 12px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #E3E3DD">
    <tr><td style="background:{INK};padding:22px 32px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
        <td style="font:700 22px {font};color:#ffffff;letter-spacing:-.01em">brn<span style="color:{BLUE_LIGHT}">.</span> <span style="font-weight:500;color:#C9CAD1">dynamics</span></td>
        <td align="right">{pill}</td>
      </tr></table>
    </td></tr>
    <tr><td style="height:4px;line-height:4px;font-size:0;background:{BLUE}">&nbsp;</td></tr>
    <tr><td style="padding:32px 32px 8px 32px;font:400 16px/1.6 {body_font};color:#1A1B20">
      {_paragraphs(body)}
      {button}
    </td></tr>
    <tr><td style="padding:0 32px 28px 32px">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #ECECE6;width:100%"><tr><td style="padding-top:18px;font:400 14px/1.5 {body_font};color:#1A1B20">
        <b style="font:700 15px {font};color:{INK}">{html.escape(sender_name)}</b><br>
        <span style="color:{MUTED}">Software &amp; AI partner · Riyadh · Kuwait City · Karachi · Geneva</span><br>
        <a href="https://brndynamics.com" style="color:{BLUE};text-decoration:none;font-weight:600">brndynamics.com</a>
      </td></tr></table>
    </td></tr>
    <tr><td style="background:{PAPER};padding:14px 32px;font:400 12px/1.5 {body_font};color:{MUTED}">
      80+ projects shipped &nbsp;·&nbsp; 5.0 on Clutch &nbsp;·&nbsp; Building since 2019
    </td></tr>
  </table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px">
    <tr><td style="padding:16px 32px;font:400 11px/1.6 {body_font};color:#8A8C94;text-align:center">
      {html.escape(address)}<br>
      You're receiving this because we thought it might be relevant to your business. Reply "stop" or
      <a href="mailto:{html.escape(sender_email)}?subject=unsubscribe" style="color:#8A8C94;text-decoration:underline">unsubscribe</a> and we won't email you again.
    </td></tr>
  </table>
</td></tr>
</table>
</body></html>"""


def text_version(body, cta_url=None, cta_label=None):
    sender_name = db.setting("sender_name") or "brn dynamics"
    address = db.setting("company_address") or "brn dynamics"
    cta = f"\n\n{cta_label or 'Book a 15-min call'}: {cta_url}" if cta_url else ""
    return (f"{body.strip()}{cta}\n\n--\n{sender_name}\nbrndynamics.com\n\n{address}\n"
            "Reply \"stop\" and we will not contact you again.")
