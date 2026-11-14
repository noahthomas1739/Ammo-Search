"""Send alerts. Configure one or more channels with environment variables
(GitHub repo secrets):

  Twilio SMS:   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, ALERT_PHONE
  ntfy push:    NTFY_TOPIC  (optional NTFY_SERVER, default https://ntfy.sh)
  Email:        SMTP_USER + SMTP_PASSWORD is enough for Gmail (use an App Password);
                mail goes to SMTP_USER unless ALERT_EMAIL is set (comma-separated list).
                Other providers: also set SMTP_HOST (and SMTP_PORT if not 587).

Set DRY_RUN=1 to print instead of sending.
"""
from __future__ import annotations

import base64
import logging
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _env(*names: str) -> bool:
    return all(os.environ.get(n) for n in names)


def channels() -> list[str]:
    out = []
    if _env("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_PHONE"):
        out.append("twilio")
    if _env("NTFY_TOPIC"):
        out.append("ntfy")
    if _env("SMTP_USER", "SMTP_PASSWORD") or _env("SMTP_HOST", "ALERT_EMAIL"):
        out.append("email")
    return out


def _chunks(text: str, size: int) -> list[str]:
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > size:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return [p[:size] for p in parts]


def _twilio(body: str) -> None:
    sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    for to in [p.strip() for p in os.environ["ALERT_PHONE"].split(",") if p.strip()]:
        for part in _chunks(body, 1500):
            data = urllib.parse.urlencode({"From": os.environ["TWILIO_FROM"], "To": to, "Body": part}).encode()
            req = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
            urllib.request.urlopen(req, timeout=30).read()


def _ntfy(title: str, body: str, urgent: bool) -> None:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    for part in _chunks(body, 3800):
        req = urllib.request.Request(
            f"{server}/{os.environ['NTFY_TOPIC']}",
            data=part.encode(),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "5" if urgent else "3", "Tags": "boom" if urgent else "memo"},
        )
        urllib.request.urlopen(req, timeout=30).read()


def _email(title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "ammo-monitor@localhost"
    msg["To"] = os.environ.get("ALERT_EMAIL") or os.environ["SMTP_USER"]
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
    with s:
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def send(title: str, body: str, urgent: bool = False) -> list[str]:
    """Send on every configured channel. Returns the channels that worked."""
    print(f"\n===== {title} =====\n{body}\n")
    if os.environ.get("DRY_RUN"):
        return ["dry-run"]
    ok = []
    chans = channels()
    if not chans:
        log.warning("No alert channel configured; message printed only. See README > Alerts.")
    for ch in chans:
        try:
            if ch == "twilio":
                _twilio(body)
            elif ch == "ntfy":
                _ntfy(title, body, urgent)
            elif ch == "email":
                _email(title, body)
            ok.append(ch)
        except Exception as e:
            log.error("Sending via %s failed: %s", ch, e)
    if chans and not ok:
        raise RuntimeError("Every alert channel failed")
    return ok
