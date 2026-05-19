"""Supabase integration: client + thin helpers for users/downloads.

If either SUPABASE_URL or SUPABASE_KEY is empty, every helper here is a no-op
and returns None / safe defaults so the server keeps working in anonymous /
DEV mode without a database.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from supabase import Client, create_client

logger = logging.getLogger("drakonrhym.db")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

_client: Client | None = None


def is_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def get_client() -> Client | None:
    """Lazily build a Supabase client. Returns None if env is not configured."""
    global _client
    if _client is None and is_enabled():
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ---------- helpers ----------


def upsert_user(
    google_id: str,
    *,
    email: str | None,
    name: str | None,
    avatar_url: str | None,
) -> dict[str, Any] | None:
    """Insert a new user, or update `last_login_at` (+ refreshed profile fields)
    for an existing one. Returns the resulting row, or None if Supabase is off."""
    client = get_client()
    if client is None:
        return None
    try:
        existing = (
            client.table("users")
            .select("*")
            .eq("google_id", google_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            row = existing.data[0]
            updates: dict[str, Any] = {"last_login_at": "now()"}
            # Refresh display fields opportunistically (user may have changed
            # their Google profile picture / name).
            if email is not None and email != row.get("email"):
                updates["email"] = email
            if name is not None and name != row.get("name"):
                updates["name"] = name
            if avatar_url is not None and avatar_url != row.get("avatar_url"):
                updates["avatar_url"] = avatar_url
            updated = (
                client.table("users")
                .update(updates)
                .eq("id", row["id"])
                .execute()
            )
            return updated.data[0] if updated.data else row

        inserted = (
            client.table("users")
            .insert(
                {
                    "google_id": google_id,
                    "email": email,
                    "name": name,
                    "avatar_url": avatar_url,
                }
            )
            .execute()
        )
        return inserted.data[0] if inserted.data else None
    except Exception:
        logger.exception("upsert_user failed for google_id=%s", google_id)
        return None


def get_user_by_google_id(google_id: str) -> dict[str, Any] | None:
    client = get_client()
    if client is None:
        return None
    try:
        res = (
            client.table("users")
            .select("*")
            .eq("google_id", google_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("get_user_by_google_id failed for %s", google_id)
        return None


def consume_quota(user_id: str) -> dict[str, Any] | None:
    """Atomically consume one daily-quota slot via RPC.

    Returns {'allowed': bool, 'used': int, 'limit': int}, or None if Supabase
    is off (caller should treat None as 'allowed, unknown').
    """
    client = get_client()
    if client is None:
        return None
    try:
        res = client.rpc("consume_quota", {"p_user_id": user_id}).execute()
        return res.data
    except Exception:
        logger.exception("consume_quota RPC failed for %s", user_id)
        return None


def refund_quota(user_id: str) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.rpc("refund_quota", {"p_user_id": user_id}).execute()
    except Exception:
        logger.exception("refund_quota RPC failed for %s", user_id)


def record_download(
    *,
    user_id: str,
    youtube_url: str,
    semitones: int,
    cents: int,
    status: str,
) -> None:
    """Insert a row into `downloads`. status must be 'success' or 'failed'."""
    client = get_client()
    if client is None:
        return
    try:
        client.table("downloads").insert(
            {
                "user_id": user_id,
                "youtube_url": youtube_url,
                "semitones": semitones,
                "cents": cents,
                "status": status,
            }
        ).execute()
    except Exception:
        logger.exception("record_download failed for user=%s url=%s", user_id, youtube_url)


def split_pitch_to_semitones_cents(pitch: float) -> tuple[int, int]:
    """Split a signed semitone float (e.g. 2.5 → (2, 50), -1.3 → (-1, -30))."""
    if pitch == 0:
        return 0, 0
    sign = -1 if pitch < 0 else 1
    abs_pitch = abs(pitch)
    semitones = int(abs_pitch)
    cents = round((abs_pitch - semitones) * 100)
    return semitones * sign, cents * sign
