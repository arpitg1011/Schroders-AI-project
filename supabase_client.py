"""supabase_client.py - Phase 2: Supabase auth + saved-portfolio persistence.

Fully isolated from the Phase 1 analysis path: engine.py and the core UI never
import this module, so ThemeIQ runs with no Supabase config (Req 11.5, 11.7).

All functions follow the engine's {"ok": bool, ...} convention so the UI can
render handled errors without crashing. The generative/analysis path never
depends on anything here.
"""

from __future__ import annotations

import json

from engine import get_secret  # reuses st.secrets -> .env resolution


# ---------------------------------------------------------------------------
# Result helpers (mirror engine.err/ok so the UI handling is uniform)
# ---------------------------------------------------------------------------
def _ok(**payload) -> dict:
    return {"ok": True, **payload}


def _err(code: str, message: str, detail: str | None = None) -> dict:
    return {"ok": False, "error_code": code, "message": message, "detail": detail}


# ---------------------------------------------------------------------------
# Client (created lazily; cached per URL+key)
# ---------------------------------------------------------------------------
_client = None


def is_configured() -> bool:
    """True when both Supabase settings are present (enables the auth UI)."""
    return bool(get_secret("SUPABASE_URL") and get_secret("SUPABASE_ANON_KEY"))


def _get_client():
    """Return a cached Supabase client, or None if not configured/unavailable."""
    global _client
    if _client is not None:
        return _client
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client  # lazy import
        _client = create_client(url, key)
        return _client
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Authentication (Req 11)
# ---------------------------------------------------------------------------
def sign_up(email: str, password: str) -> dict:
    """Register a new user. With email-confirmation disabled, a session is
    returned immediately. Returns {ok, user, session} or a handled error."""
    client = _get_client()
    if client is None:
        return _err("SUPABASE_UNCONFIGURED", "Login is not configured for this deployment.")
    try:
        res = client.auth.sign_up({"email": email, "password": password})
        if res.user is None:
            return _err("SIGNUP_FAILED", "Could not create the account.")
        return _ok(user=_user_dict(res.user), session=_session_dict(res.session))
    except Exception as exc:
        return _err("SIGNUP_FAILED", _friendly(exc), detail=str(exc))


def sign_in(email: str, password: str) -> dict:
    """Log in with email + password. Returns {ok, user, session} or error."""
    client = _get_client()
    if client is None:
        return _err("SUPABASE_UNCONFIGURED", "Login is not configured for this deployment.")
    try:
        res = client.auth.sign_in_with_password({"email": email, "password": password})
        if res.user is None or res.session is None:
            return _err("INVALID_CREDENTIALS", "Incorrect email or password.")
        return _ok(user=_user_dict(res.user), session=_session_dict(res.session))
    except Exception as exc:
        return _err("INVALID_CREDENTIALS", _friendly(exc), detail=str(exc))


def sign_out() -> dict:
    client = _get_client()
    if client is None:
        return _err("SUPABASE_UNCONFIGURED", "Login is not configured.")
    try:
        client.auth.sign_out()
    except Exception:
        pass  # local sign-out is best-effort
    return _ok()


def _restore_session(access_token: str, refresh_token: str) -> bool:
    """Re-attach a stored session to the client so RLS applies to queries."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.auth.set_session(access_token, refresh_token)
        return True
    except Exception:
        return False


def _user_dict(user) -> dict:
    return {"id": user.id, "email": getattr(user, "email", None)}


def _session_dict(session) -> dict:
    if session is None:
        return {}
    return {"access_token": session.access_token, "refresh_token": session.refresh_token}


def _friendly(exc) -> str:
    msg = str(exc).lower()
    if "already registered" in msg or "already been registered" in msg:
        return "That email is already registered. Try logging in instead."
    if "invalid login" in msg or "credentials" in msg:
        return "Incorrect email or password."
    if "password" in msg and "least" in msg:
        return "Password is too short (minimum 6 characters)."
    return "Authentication failed. Please check your details and try again."


# ---------------------------------------------------------------------------
# Saved portfolios (Req 12) - parsed rows stored as JSON under the user profile
# ---------------------------------------------------------------------------
def save_portfolio(session: dict, name: str, portfolio_df) -> dict:
    """Upsert the user's portfolio (parsed rows as JSON). Keyed by (user, name)."""
    client = _get_client()
    if client is None:
        return _err("SUPABASE_UNCONFIGURED", "Saving is not configured.")
    if not _restore_session(session.get("access_token", ""), session.get("refresh_token", "")):
        return _err("NO_SESSION", "Your session expired. Please log in again.")
    try:
        user = client.auth.get_user()
        uid = user.user.id
        rows = json.loads(portfolio_df.to_json(orient="records"))
        payload = {"user_id": uid, "name": name, "contents": rows}
        client.table("saved_portfolios").upsert(payload, on_conflict="user_id,name").execute()
        return _ok()
    except Exception as exc:
        return _err("SAVE_FAILED", "Could not save the portfolio.", detail=str(exc))


def load_latest_portfolio(session: dict):
    """Return the user's most recently updated saved portfolio as a DataFrame,
    or {ok: True, portfolio: None} when they have none."""
    import pandas as pd
    client = _get_client()
    if client is None:
        return _err("SUPABASE_UNCONFIGURED", "Loading is not configured.")
    if not _restore_session(session.get("access_token", ""), session.get("refresh_token", "")):
        return _err("NO_SESSION", "Your session expired. Please log in again.")
    try:
        res = (client.table("saved_portfolios")
               .select("name, contents, updated_at")
               .order("updated_at", desc=True)
               .limit(1).execute())
        data = res.data or []
        if not data:
            return _ok(portfolio=None, name=None)
        row = data[0]
        df = pd.DataFrame(row["contents"])
        return _ok(portfolio=df, name=row.get("name"))
    except Exception as exc:
        return _err("LOAD_FAILED", "Could not load your saved portfolio.", detail=str(exc))
