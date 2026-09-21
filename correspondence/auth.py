import os
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from threading import RLock


GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
JWT_SECRET           = os.getenv("JWT_SECRET") or secrets.token_hex(32)
JWT_ALGORITHM        = "HS256"
JWT_EXPIRE_DAYS      = 30
BASE_URL             = os.getenv("BASE_URL", "http://localhost:8000")

# Only allow OAuth over plain HTTP in local dev — never in production
if BASE_URL.startswith("http://localhost") or BASE_URL.startswith("http://127."):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
else:
    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
REDIRECT_URI         = f"{BASE_URL}/auth/google/callback"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Store the Flow object per state so PKCE code_verifier is preserved.
# Entries are tuples of (flow, created_at). Stale entries are evicted on
# access — abandoned flows (user closes the Google tab, OAuth error page,
# bad redirect URI) would otherwise accumulate forever and slowly leak memory.
_PENDING_FLOW_TTL_SECONDS = 600  # 10 minutes is plenty for a real flow
_pending_flows: dict[str, tuple] = {}
_pending_flows_lock = RLock()


def _evict_stale_flows():
    """Drop entries older than the TTL. Called on every get/set."""
    cutoff = time.time() - _PENDING_FLOW_TTL_SECONDS
    with _pending_flows_lock:
        stale = [k for k, (_, ts) in _pending_flows.items() if ts < cutoff]
        for k in stale:
            _pending_flows.pop(k, None)


def _build_flow():
    from google_auth_oauthlib.flow import Flow  # lazy: only on sign-in
    return Flow.from_client_config(
        client_config={
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def get_auth_url():
    """Returns (auth_url, state) to redirect the browser to Google."""
    _evict_stale_flows()
    flow = _build_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    with _pending_flows_lock:
        _pending_flows[state] = (flow, time.time())
    return auth_url, state


def exchange_code(code, state):
    """
    Exchange authorization code → (id_info dict, refresh_token str).
    Raises ValueError on bad state.
    """
    _evict_stale_flows()
    with _pending_flows_lock:
        entry = _pending_flows.pop(state, None)
    if entry is None:
        raise ValueError("Invalid OAuth state — server may have restarted mid-flow")
    flow, _ = entry

    flow.fetch_token(code=code)

    credentials = flow.credentials

    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests

    id_info = id_token.verify_oauth2_token(
        credentials.id_token,
        google_requests.Request(),
        GOOGLE_CLIENT_ID,
    )

    return id_info, credentials.refresh_token


def make_user_id(google_sub: str) -> str:
    return hashlib.sha256(f"google:{google_sub}".encode()).hexdigest()[:32]


def issue_jwt(user_id: str) -> str:
    from jose import jwt
    payload = {
        "sub": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt(token: str) -> str:
    """Returns user_id or raises ValueError."""
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload["sub"]
    except JWTError as e:
        raise ValueError(f"Invalid token: {e}")
