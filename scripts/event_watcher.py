"""
Event watcher — daily job that detects meaningful bill state changes
and emails active subscribers.

Run via POST /watcher/run (protected by WATCHER_SECRET) or directly:
    python event_watcher.py
"""

import os
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from dotenv import load_dotenv

load_dotenv()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
BASE_URL = os.getenv("BASE_URL", "https://nospopuli-production.up.railway.app")

NOTIFY_FROM    = os.getenv("NOTIFY_FROM_EMAIL", "")
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASS      = os.getenv("SMTP_PASS", "")

_EMAIL_ENABLED = bool(NOTIFY_FROM and SMTP_USER and SMTP_PASS)

# ── Bill state normalization ──

STATE_PRIORITY = {
    "introduced":      0,
    "in_committee":    1,
    "passed_committee": 2,
    "floor_scheduled": 3,
    "passed_house":    4,
    "failed_house":    4,
    "passed_senate":   5,
    "failed_senate":   5,
    "to_president":    6,
    "signed":          7,
    "vetoed":          7,
}

_PATTERNS = [
    ("signed",          re.compile(r"signed by president|became public law", re.I)),
    ("vetoed",          re.compile(r"vetoed by president|pocket veto", re.I)),
    ("to_president",    re.compile(r"presented to president|sent to president", re.I)),
    ("passed_senate",   re.compile(r"passed senate|on passage.*senate.*passed", re.I)),
    ("failed_senate",   re.compile(r"failed.*senate|rejected.*senate", re.I)),
    ("passed_house",    re.compile(r"passed house|on passage.*house.*passed", re.I)),
    ("failed_house",    re.compile(r"failed.*house|rejected.*house", re.I)),
    ("floor_scheduled", re.compile(r"placed on.*calendar|scheduled for floor", re.I)),
    ("passed_committee", re.compile(r"ordered to be reported|committee discharged", re.I)),
    ("in_committee",    re.compile(r"referred to (the )?committee|referred to subcommittee", re.I)),
    ("introduced",      re.compile(r"introduced in (house|senate)", re.I)),
]


def normalize_bill_state(actions):
    """
    Returns the most advanced state name from the list of raw action dicts.
    Falls back to 'introduced' if nothing matches.
    """
    best = "introduced"
    best_priority = -1
    for action in (actions or []):
        desc = action.get("description") or action.get("text") or ""
        for state, pattern in _PATTERNS:
            if pattern.search(desc):
                p = STATE_PRIORITY.get(state, 0)
                if p > best_priority:
                    best = state
                    best_priority = p
    return best


# ── Event classifier ──

# (old_state, new_state) → human-readable event label, or None to suppress
_NOTIFIABLE_TRANSITIONS = {
    ("*", "passed_house"):    "passed the House",
    ("*", "failed_house"):    "failed in the House",
    ("*", "passed_senate"):   "passed the Senate",
    ("*", "failed_senate"):   "failed in the Senate",
    ("*", "to_president"):    "been sent to the President",
    ("*", "signed"):          "been signed into law",
    ("*", "vetoed"):          "been vetoed by the President",
    ("*", "passed_committee"): "passed out of committee",
}


def classify_event(old_state, new_state):
    """
    Returns a short event phrase if the transition is notifiable, else None.
    old_state may be None (first time we've seen this bill).
    """
    if old_state == new_state:
        return None
    old_priority = STATE_PRIORITY.get(old_state or "introduced", -1)
    new_priority = STATE_PRIORITY.get(new_state, 0)
    if new_priority <= old_priority:
        return None
    return _NOTIFIABLE_TRANSITIONS.get(("*", new_state))


# ── Email ──

def _bill_url(bill_id):
    return f"{BASE_URL}/?bill={requests.utils.quote(bill_id)}"


def _unsubscribe_url(email, bill_id):
    return (f"{BASE_URL}/correspondence/unsubscribe-link"
            f"?email={requests.utils.quote(email)}&bill_id={requests.utils.quote(bill_id)}")


def send_notification(to_email, bill_id, bill_title, event_phrase, kind="bill"):
    subject = f"Update on {bill_id}"
    bill_link  = _bill_url(bill_id)
    unsub_link = _unsubscribe_url(to_email, bill_id)
    verb = "This place has" if kind == "place" else "This bill has"

    text_body = (
        f"{bill_id} — {bill_title}\n\n"
        f"{verb} {event_phrase}.\n\n"
        f"View: {bill_link}\n\n"
        f"—\nYou're receiving this because you subscribed to updates on NosPopuli.\n"
        f"Unsubscribe: {unsub_link}"
    )
    html_body = f"""
<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;color:#0e0e0e">
  <p style="font-size:0.75rem;color:#6b6355;letter-spacing:0.08em;text-transform:uppercase">
    NosPopuli · {"Place Update" if kind == "place" else "Bill Update"}
  </p>
  <h2 style="margin:0.5rem 0 0.25rem">{bill_id}</h2>
  <p style="margin:0 0 1.5rem;color:#6b6355">{bill_title}</p>
  <p style="font-size:1.05rem">{verb} <strong>{event_phrase}</strong>.</p>
  <p style="margin-top:1.5rem">
    <a href="{bill_link}"
       style="background:#8b1a1a;color:#fff;padding:0.5rem 1.2rem;text-decoration:none;
              font-family:'IBM Plex Mono',monospace;font-size:0.75rem;letter-spacing:0.1em;
              text-transform:uppercase">
      View →
    </a>
  </p>
  <hr style="margin:2rem 0;border:none;border-top:1px solid #c8bfaa">
  <p style="font-size:0.7rem;color:#6b6355">
    You subscribed to updates via NosPopuli.<br>
    <a href="{unsub_link}" style="color:#6b6355">Unsubscribe</a>
  </p>
</div>"""

    if not _EMAIL_ENABLED:
        print(f"[WATCHER] Email disabled — would notify {to_email}: {bill_id} has {event_phrase}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = NOTIFY_FROM
    msg["To"]      = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(NOTIFY_FROM, to_email, msg.as_string())
        print(f"[WATCHER] Notified {to_email}: {bill_id} has {event_phrase}")
    except Exception as e:
        print(f"[WATCHER] Email error for {to_email}: {e}")


# ── Watcher loop ──

def _fetch_actions(congress, bill_type, bill_number):
    url = (f"https://api.congress.gov/v3/bill"
           f"/{congress}/{bill_type}/{bill_number}/actions")
    try:
        r = requests.get(url, params={"api_key": CONGRESS_API_KEY,
                                      "format": "json", "limit": 30}, timeout=10)
        if r.status_code == 200:
            return r.json().get("actions", [])
    except Exception as e:
        print(f"[WATCHER] Actions fetch error: {e}")
    return []


def _run_place_watches():
    """Email place-watch subscribers once Foundry certifies that county."""
    from correspondence.db import get_place_subscriptions, update_subscription_state
    from agents.ledger_agent import foundry_place_coverage

    rows = get_place_subscriptions()
    sent = 0
    for sub in rows:
        bill_id = sub.get("bill_id") or ""
        slug = bill_id.split(":", 1)[-1]
        if not slug:
            continue
        cov = foundry_place_coverage(slug)
        if not cov.get("certified"):
            continue
        if sub.get("last_notified_state") == "charted":
            continue
        send_notification(
            sub["email"],
            bill_id,
            sub.get("bill_title") or slug,
            "been charted — local votes are now on the record",
            kind="place",
        )
        update_subscription_state(sub["email"], bill_id, "charted")
        sent += 1
    return sent


def run_watcher():
    from correspondence.db import get_active_subscribed_bills, get_subscriptions_for_bill, update_subscription_state

    bills = get_active_subscribed_bills()
    print(f"[WATCHER] Checking {len(bills)} subscribed bills")

    notifications_sent = 0

    for bill in bills:
        congress    = bill["congress"]
        bill_type   = bill["bill_type"]
        bill_number = bill["bill_number"]
        bill_id     = bill["bill_id"]
        bill_title  = bill["bill_title"] or bill_id

        actions = _fetch_actions(congress, bill_type, bill_number)
        if not actions:
            continue

        new_state = normalize_bill_state(actions)
        subscribers = get_subscriptions_for_bill(bill_id)

        for sub in subscribers:
            old_state   = sub["last_notified_state"]
            event_phrase = classify_event(old_state, new_state)

            if event_phrase:
                send_notification(sub["email"], bill_id, bill_title, event_phrase)
                notifications_sent += 1

            if old_state != new_state:
                update_subscription_state(sub["email"], bill_id, new_state)

    try:
        notifications_sent += _run_place_watches()
    except Exception as e:
        print(f"[WATCHER] Place watches skipped: {e}")

    print(f"[WATCHER] Done. {notifications_sent} notification(s) sent.")
    return {"bills_checked": len(bills), "notifications_sent": notifications_sent}


if __name__ == "__main__":
    run_watcher()
