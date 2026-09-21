import os
import uuid
import asyncio
from datetime import datetime
from typing import Optional

import anthropic
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .db import (
    init_db, upsert_user, get_user,
    update_user_gmail, update_user_zip,
    check_rate_limit, increment_rate_limit, check_bill_rep_cooldown,
    save_correspondence, get_user_correspondence,
    get_correspondence_by_id, save_reply, get_replies,
    upsert_subscription, get_subscription, deactivate_subscription,
    get_subscriptions_for_email,
)
from .auth import get_auth_url, exchange_code, make_user_id, issue_jwt, verify_jwt
from .gmail import screen_email_username, send_email, check_thread_replies, FOOTER
from .draft import generate_draft, moderate_email, generate_followup

router = APIRouter()

# init_db() needs to be tolerant of startup failures — if the database can't
# be created for any reason (read-only filesystem, missing parent dir,
# permission issue), we must NOT crash the whole FastAPI app. Auth/correspondence
# will return 503 instead, while the rest of the app keeps serving.
try:
    init_db()
    _DB_READY = True
except Exception as _db_err:
    print(f"[CORRESPONDENCE] init_db failed at startup: {_db_err}")
    _DB_READY = False

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _claude():
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        user_id = verify_jwt(auth[7:])
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── OAuth ──

@router.get("/auth/google")
async def auth_google():
    if not _DB_READY:
        raise HTTPException(status_code=503, detail="Auth subsystem unavailable")
    if not os.getenv("GOOGLE_CLIENT_ID") or not os.getenv("GOOGLE_CLIENT_SECRET"):
        raise HTTPException(
            status_code=503,
            detail="Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in env."
        )
    if not os.getenv("BASE_URL", "").startswith(("http://", "https://")):
        raise HTTPException(
            status_code=503,
            detail="BASE_URL not set — OAuth redirect cannot be computed."
        )
    try:
        auth_url, _ = get_auth_url()
    except Exception as e:
        # Bad credential format, network issue talking to Google's discovery
        # endpoint, etc. Return a clean 503 instead of letting the exception
        # bubble up the stack.
        print(f"[AUTH] Failed to build auth URL: {e}")
        raise HTTPException(
            status_code=503,
            detail="Could not start Google sign-in. Check server config."
        )
    return RedirectResponse(auth_url)


@router.get("/auth/google/callback")
async def auth_google_callback(code: str, state: str):
    loop = asyncio.get_event_loop()

    try:
        id_info, refresh_token = await loop.run_in_executor(
            None, exchange_code, code, state
        )
    except Exception as e:
        print(f"[AUTH] Exchange failed: {e}")
        return RedirectResponse(f"{BASE_URL}/?auth_error=oauth")

    google_sub = id_info["sub"]
    email      = id_info.get("email", "")
    name       = id_info.get("name", "")

    user_id = make_user_id(google_sub)
    upsert_user(user_id, email, name)

    screened = False
    if refresh_token:
        try:
            approved, _ = await loop.run_in_executor(
                None, screen_email_username, email, _claude()
            )
            screened = approved
            update_user_gmail(user_id, email, refresh_token, screened)
        except Exception as e:
            print(f"[AUTH] Screening failed: {e}")

    token = issue_jwt(user_id)
    flag = "1" if screened else "0"
    return RedirectResponse(f"{BASE_URL}/?np_token={token}&email_ok={flag}")


@router.get("/auth/me")
async def auth_me(request: Request):
    user = _current_user(request)
    return {
        "id":              user["id"],
        "email":           user["email"],
        "name":            user["name"],
        "zip_code":        user["zip_code"],
        "state":           user["state"],
        "gmail_address":   user["gmail_address"],
        "email_screened":  bool(user["email_screened"]),
        "gmail_connected": bool(user["gmail_refresh_token"]),
    }


# ── User zip ──

class ZipUpdateRequest(BaseModel):
    zip_code: str
    state: str
    city: Optional[str] = ""


@router.post("/user/zip")
async def save_user_zip(body: ZipUpdateRequest, request: Request):
    user = _current_user(request)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, update_user_zip, user["id"], body.zip_code, body.state, body.city or ""
    )
    return {"ok": True}


# ── Draft ──

class DraftRequest(BaseModel):
    bill_id: str
    bill_title: str
    bill_summary: str
    latest_action: Optional[str] = ""
    legislator_name: str
    legislator_office: str
    user_statement: str
    full_name: Optional[str] = ""


@router.post("/correspondence/draft")
async def draft_correspondence(body: DraftRequest, request: Request):
    user = _current_user(request)

    if not user["email_screened"]:
        raise HTTPException(status_code=403, detail="Gmail address not approved for correspondence")

    if not check_rate_limit(user["id"], "draft", 10):
        raise HTTPException(status_code=429, detail="Draft limit reached for today (10/day)")

    loop = asyncio.get_event_loop()
    draft = await loop.run_in_executor(
        None, generate_draft,
        body.bill_id, body.bill_title, body.bill_summary, body.latest_action,
        body.legislator_name, body.legislator_office,
        user.get("city") or "", user.get("state") or "",
        body.user_statement, body.full_name or "", _claude()
    )

    increment_rate_limit(user["id"], "draft")

    subject = f"{body.bill_id} — {body.bill_title[:60]}"
    return {"draft": draft, "subject": subject, "footer": FOOTER}


# ── Send ──

class SendRequest(BaseModel):
    bill_id: str
    bill_title: str
    legislator_name: str
    legislator_office: str
    legislator_state: Optional[str] = ""
    to_email: Optional[str] = None
    contact_form_url: Optional[str] = None
    subject: str
    body: str


@router.post("/correspondence/send")
async def send_correspondence(body: SendRequest, request: Request):
    user = _current_user(request)

    if not user["email_screened"]:
        raise HTTPException(status_code=403, detail="Gmail address not approved")
    if not user["gmail_refresh_token"]:
        raise HTTPException(status_code=403, detail="Gmail not connected")

    if not check_rate_limit(user["id"], "send", 5):
        raise HTTPException(status_code=429, detail="Send limit reached for today (5/day)")

    if check_bill_rep_cooldown(user["id"], body.bill_id, body.legislator_name):
        raise HTTPException(
            status_code=429,
            detail=f"You already contacted {body.legislator_name} about this bill within the last 30 days"
        )

    loop = asyncio.get_event_loop()

    ok, reason = await loop.run_in_executor(
        None, moderate_email, body.body, body.bill_id, _claude()
    )
    if not ok:
        raise HTTPException(status_code=400, detail=f"Message blocked: {reason}")

    corr_id    = str(uuid.uuid4())
    thread_id  = None
    message_id = None
    delivery   = "form"

    if body.to_email:
        try:
            thread_id, message_id = await loop.run_in_executor(
                None, send_email,
                user["gmail_refresh_token"], body.to_email, body.subject, body.body
            )
            delivery = "email"
        except Exception as e:
            print(f"[SEND] Gmail error: {e}")
            raise HTTPException(status_code=500, detail="Failed to send via Gmail. Please try again.")

    save_correspondence({
        "id":               corr_id,
        "user_id":          user["id"],
        "bill_id":          body.bill_id,
        "bill_title":       body.bill_title,
        "legislator_name":  body.legislator_name,
        "legislator_office": body.legislator_office,
        "legislator_state": body.legislator_state or "",
        "to_email":         body.to_email,
        "contact_form_url": body.contact_form_url,
        "subject":          body.subject,
        "body":             body.body,
        "sent_at":          datetime.utcnow().isoformat(),
        "delivery_method":  delivery,
        "gmail_thread_id":  thread_id,
        "gmail_message_id": message_id,
    })

    # Implicit subscription: sending a letter auto-subscribes the user
    upsert_subscription(
        email=user["email"],
        bill_id=body.bill_id,
        bill_title=body.bill_title,
        source='letter',
        user_id=user["id"],
        congress=getattr(body, 'congress', None),
        bill_type=getattr(body, 'bill_type', None),
        bill_number=getattr(body, 'bill_number', None),
    )

    increment_rate_limit(user["id"], "send")

    return {
        "ok":               True,
        "correspondence_id": corr_id,
        "delivery_method":  delivery,
        "contact_form_url": body.contact_form_url if delivery == "form" else None,
    }


# ── List & replies ──

@router.get("/correspondence")
async def list_correspondence(request: Request):
    user = _current_user(request)
    items = get_user_correspondence(user["id"])
    return {"items": items}


@router.get("/correspondence/{corr_id}/replies")
async def check_replies(corr_id: str, request: Request):
    user = _current_user(request)
    corr = get_correspondence_by_id(corr_id, user["id"])
    if not corr:
        raise HTTPException(status_code=404, detail="Not found")

    if corr["delivery_method"] != "email" or not corr.get("gmail_thread_id"):
        return {"replies": get_replies(corr_id)}

    if not user["gmail_refresh_token"]:
        return {"replies": get_replies(corr_id)}

    loop = asyncio.get_event_loop()
    try:
        new_msgs = await loop.run_in_executor(
            None, check_thread_replies,
            user["gmail_refresh_token"],
            corr["gmail_thread_id"],
            corr.get("gmail_message_id"),
        )
        for m in new_msgs:
            save_reply(corr_id, m["id"], m.get("date", ""), m.get("preview", ""))
    except Exception as e:
        print(f"[REPLIES] Gmail fetch error: {e}")

    return {"replies": get_replies(corr_id)}


# ── Subscriptions ──

class SubscribeRequest(BaseModel):
    bill_id: str
    bill_title: Optional[str] = ""
    email: Optional[str] = None        # required for anonymous callers
    congress: Optional[int] = None
    bill_type: Optional[str] = None
    bill_number: Optional[int] = None
    ocd_id: Optional[str] = None


@router.post("/correspondence/subscribe")
async def subscribe(body: SubscribeRequest, request: Request):
    token = request.headers.get("Authorization", "")
    user = None
    if token.startswith("Bearer "):
        try:
            user_id = verify_jwt(token[7:])
            user = get_user(user_id)
        except Exception:
            pass

    email = (user["email"] if user else None) or body.email
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, upsert_subscription,
        email, body.bill_id, body.bill_title or "", "manual",
        user["id"] if user else None,
        body.congress, body.bill_type, body.bill_number, body.ocd_id)

    return {"ok": True, "subscribed": True}


class UnsubscribeRequest(BaseModel):
    bill_id: str
    email: str


@router.post("/correspondence/unsubscribe")
async def unsubscribe(body: UnsubscribeRequest, request: Request):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, deactivate_subscription, body.email, body.bill_id)
    return {"ok": True, "subscribed": False}


@router.get("/correspondence/subscription")
async def subscription_status(bill_id: str, email: str):
    sub = get_subscription(email, bill_id)
    return {"subscribed": bool(sub and sub["active"])}


@router.get("/correspondence/subscriptions")
async def list_subscriptions(email: str):
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, get_subscriptions_for_email, email) or []
    return {
        "watching": [
            {
                "id": r["bill_id"],
                "title": r.get("bill_title") or r["bill_id"],
                "congress": r.get("congress"),
                "bill_type": r.get("bill_type"),
                "bill_number": r.get("bill_number"),
            }
            for r in rows
        ]
    }


# ── Follow-up draft ──

class FollowupRequest(BaseModel):
    correspondence_id: str
    reply_text: str


@router.post("/correspondence/followup/draft")
async def draft_followup(body: FollowupRequest, request: Request):
    user = _current_user(request)
    corr = get_correspondence_by_id(body.correspondence_id, user["id"])
    if not corr:
        raise HTTPException(status_code=404, detail="Correspondence not found")

    if not check_rate_limit(user["id"], "draft", 10):
        raise HTTPException(status_code=429, detail="Draft limit reached for today")

    loop = asyncio.get_event_loop()
    draft = await loop.run_in_executor(
        None, generate_followup,
        corr["body"], body.reply_text, corr["legislator_name"], _claude()
    )

    increment_rate_limit(user["id"], "draft")

    return {
        "draft": draft,
        "subject": f"Re: {corr['subject']}",
        "footer": FOOTER,
    }
