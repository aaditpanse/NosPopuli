import os
import base64
from email.mime.text import MIMEText


GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
TOKEN_URI            = "https://oauth2.googleapis.com/token"

FOOTER = (
    "\n\n—\n"
    "Sent via NosPopuli (nospopuli.com) — civic intelligence platform.\n"
    "This message was written and approved by the constituent above."
)


def _service(refresh_token):
    # Imported here: googleapiclient + google.oauth2 cost ~8 MB and are only
    # needed once someone actually sends mail.
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri=TOKEN_URI,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def screen_email_username(gmail_address: str, claude_client) -> tuple[bool, str]:
    """
    Screen the local-part of a Gmail address for appropriateness.
    Returns (approved, reason).
    """
    local = gmail_address.split("@")[0]

    msg = claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": (
                "The following is the username portion of a Gmail address that will be used\n"
                "to send letters to elected officials on a civic platform.\n\n"
                f"Username: {local}\n\n"
                "Is this username appropriate for civic correspondence with government officials?\n"
                "Flag it if it contains: profanity, slurs, sexual language, references to violence,\n"
                "clearly juvenile or troll-style names (e.g. bigfart42, hitlerwasgood, xXpwnU420Xx).\n"
                "Allow: real names, initials, professional handles, names with numbers.\n\n"
                "Reply with exactly APPROVED or FLAGGED, then a space, then one short reason."
            )
        }]
    )

    text = msg.content[0].text.strip()
    approved = text.upper().startswith("APPROVED")
    reason = text[len("FLAGGED"):].strip() if not approved else ""
    return approved, reason


def send_email(refresh_token: str, to_email: str, subject: str, body: str) -> tuple[str, str]:
    """
    Send an email. Appends the mandatory NosPopuli footer.
    Returns (thread_id, message_id).
    """
    svc = _service(refresh_token)

    full_body = body + FOOTER
    msg = MIMEText(full_body, "plain")
    msg["to"] = to_email
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()

    return result.get("threadId", ""), result.get("id", "")


def check_thread_replies(refresh_token: str, thread_id: str, sent_message_id: str) -> list[dict]:
    """
    Check a Gmail thread for replies beyond the sent message.
    Returns list of {id, from, date, preview}.
    """
    svc = _service(refresh_token)

    thread = svc.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Date", "Subject"]
    ).execute()

    messages = thread.get("messages", [])
    new_msgs = []

    for m in messages:
        if m["id"] == sent_message_id:
            continue
        headers = {h["name"]: h["value"] for h in m.get("payload", {}).get("headers", [])}
        new_msgs.append({
            "id": m["id"],
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
        })

    if not new_msgs:
        return []

    # Fetch preview of the first reply
    full = svc.users().messages().get(
        userId="me", id=new_msgs[0]["id"], format="full"
    ).execute()
    new_msgs[0]["preview"] = full.get("snippet", "")[:200]

    return new_msgs
