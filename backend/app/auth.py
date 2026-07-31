"""Google OAuth 2.0 login and the signed-cookie session.

The flow is deliberately backend-driven: the browser never handles a token, and
the session lands in an httpOnly cookie. Because CloudFront serves the SPA and
proxies `/api/*` to this service, the cookie is first-party and needs no CORS
gymnastics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Cookie, Depends, HTTPException, status

from . import db
from .config import settings

log = logging.getLogger(__name__)

SESSION_COOKIE = "sentellent_session"
STATE_COOKIE = "sentellent_oauth_state"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Login only. The brief explicitly rules out Gmail/Calendar scopes.
GOOGLE_SCOPES = "openid email profile"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    name: str | None
    picture_url: str | None


# --------------------------------------------------------------------------- #
# Session tokens
# --------------------------------------------------------------------------- #
def issue_session(user_id: str, email: str) -> str:
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + settings.session_ttl_hours * 3600,
    }
    return jwt.encode(payload, settings.session_secret, algorithm="HS256")


def _decode_session(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, settings.session_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        log.debug("rejected session token: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# CSRF state for the OAuth round-trip
# --------------------------------------------------------------------------- #
def make_state() -> str:
    """Random nonce, HMAC'd so we can verify it without server-side storage."""
    nonce = secrets.token_urlsafe(24)
    sig = hmac.new(settings.session_secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()[
        :32
    ]
    return f"{nonce}.{sig}"


def verify_state(state: str | None, cookie_state: str | None) -> bool:
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return False
    try:
        nonce, sig = state.rsplit(".", 1)
    except ValueError:
        return False
    expected = hmac.new(
        settings.session_secret.encode(), nonce.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


# --------------------------------------------------------------------------- #
# Google endpoints
# --------------------------------------------------------------------------- #
def build_authorize_url(state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_profile(code: str) -> dict[str, Any]:
    """Swap the auth code for tokens, then read the user's basic profile."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            log.error("google token exchange failed: %s", token_resp.text[:400])
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google token exchange failed")
        tokens = token_resp.json()

        # The id_token already carries email/name; fetching userinfo keeps this
        # robust if Google trims id_token claims for a given client config.
        profile: dict[str, Any] = {}
        id_token = tokens.get("id_token")
        if id_token:
            profile.update(_decode_id_token_claims(id_token))

        access_token = tokens.get("access_token")
        if access_token:
            info = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info.status_code == 200:
                profile.update(info.json())

    if not profile.get("email"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google profile has no email")
    return profile


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Read claims from the id_token payload.

    Signature verification is intentionally skipped: the token came straight
    from Google's token endpoint over TLS in a direct server-to-server call,
    which is the condition under which RFC 8252 / OIDC allow it.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- #
# User persistence
# --------------------------------------------------------------------------- #
async def upsert_user(profile: dict[str, Any]) -> CurrentUser:
    row = await db.fetchrow(
        """
        INSERT INTO users (google_sub, email, name, picture_url, last_login_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (email) DO UPDATE
            SET google_sub    = COALESCE(EXCLUDED.google_sub, users.google_sub),
                name          = COALESCE(EXCLUDED.name, users.name),
                picture_url   = COALESCE(EXCLUDED.picture_url, users.picture_url),
                last_login_at = now()
        RETURNING id, email, name, picture_url
        """,
        profile.get("sub"),
        profile["email"].lower().strip(),
        profile.get("name"),
        profile.get("picture"),
    )
    assert row is not None
    return CurrentUser(
        id=str(row["id"]),
        email=row["email"],
        name=row["name"],
        picture_url=row["picture_url"],
    )


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
async def get_current_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> CurrentUser:
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    claims = _decode_session(session)
    if not claims:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")

    row = await db.fetchrow(
        "SELECT id, email, name, picture_url FROM users WHERE id = $1::uuid",
        claims["sub"],
    )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return CurrentUser(
        id=str(row["id"]),
        email=row["email"],
        name=row["name"],
        picture_url=row["picture_url"],
    )


async def get_optional_user(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> CurrentUser | None:
    try:
        return await get_current_user(session)
    except HTTPException:
        return None


CurrentUserDep = Depends(get_current_user)
