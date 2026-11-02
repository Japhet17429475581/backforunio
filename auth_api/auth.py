from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict

from database.users import (
    get_user_by_token_hash,
)
from database.connection import get_pool
from privacy import ConsentRecord, validate_consents, save_consents
from rate_limit import enforce_rate_limit
from groups import directory as group_directory
from config import AUTH_TOKEN_TTL_DAYS

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class GuestAuthRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_code: str = Field(min_length=1, max_length=100)
    consents: list[ConsentRecord] = Field(default_factory=list, max_length=100)


def _new_token() -> tuple[str, str]:
    token = "unio_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, token_hash


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _user_from_token(token: str | None):
    if not token:
        raise HTTPException(401, "Требуется авторизация Unio")

    user = await get_user_by_token_hash(_token_hash(token))
    if not user:
        raise HTTPException(401, "Токен Unio недействителен или был отозван")
    return user


@router.post("/guest")
async def guest_login(data: GuestAuthRequest, request: Request):
    await enforce_rate_limit(
        key=f"auth:guest:{request.client.host if request.client else 'unknown'}",
        limit=10,
        window_seconds=60,
    )
    group_code = await group_directory.validate(data.group_code)
    # Existing Android builds keep the exact document versions they displayed.
    # A historical receipt is never relabelled as accepting the current text.
    validate_consents(data.consents, required=True, allow_legacy=True)
    token, token_hash = _new_token()

    # Account and consent evidence commit together or neither is retained.
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval("""INSERT INTO users
                (auth_mode,group_code,account_token_hash,token_created_at,token_expires_at)
                VALUES ('guest',$1,$2,now(),now()+($3::integer*interval '1 day')) RETURNING id""",
                group_code, token_hash, AUTH_TOKEN_TTL_DAYS)
            await save_consents(conn, user_id, data.consents)

    return {
        "success": True,
        "auth_mode": "guest",
        "account_token": token,
        "user": {
            "id": user_id,
            "group_code": group_code,
        },
    }

@router.get("/me")
async def me(x_unio_token: str | None = Header(default=None)):
    user = await _user_from_token(x_unio_token)
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "auth_mode": user["auth_mode"],
            "group_code": user["group_code"],
            "full_name": user["full_name"],
        },
    }


@router.post("/logout")
async def logout(x_unio_token: str | None = Header(default=None)):
    if not x_unio_token:
        return {"success": True, "logged_out": True}

    async with get_pool().acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval('SELECT id FROM users WHERE account_token_hash=$1 FOR UPDATE', _token_hash(x_unio_token))
            if user_id is not None:
                await conn.execute('DELETE FROM web_portal_sessions WHERE user_id=$1', user_id)
                await conn.execute('DELETE FROM web_push_subscriptions WHERE user_id=$1', user_id)
                await conn.execute('UPDATE users SET account_token_hash=NULL,token_revoked_at=now(),token_expires_at=NULL,updated_at=now() WHERE id=$1', user_id)
    return {"success": True, "logged_out": True}


class ConsentUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    consents: list[ConsentRecord] = Field(max_length=100)


@router.put("/consents", status_code=204)
async def record_consents(data: ConsentUpdate, x_unio_token: str | None = Header(default=None)):
    validate_consents(data.consents, allow_legacy=True)
    if not x_unio_token:
        raise HTTPException(401, "Authentication required")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval("""SELECT id FROM users WHERE account_token_hash=$1
                AND token_revoked_at IS NULL AND token_expires_at>now() FOR UPDATE""", _token_hash(x_unio_token))
            if user_id is None:
                raise HTTPException(401, "Authentication required")
            await save_consents(conn, user_id, data.consents)
    return Response(status_code=204)


@router.delete("/account", status_code=204)
async def delete_account(x_unio_token: str | None = Header(default=None)):
    # Expiry must not prevent destruction. A deleted account is an idempotent success.
    if not x_unio_token or not x_unio_token.startswith('unio_') or len(x_unio_token) > 256:
        raise HTTPException(401, "Authentication required")
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute('DELETE FROM users WHERE account_token_hash=$1', _token_hash(x_unio_token))
    # FK cascades remove consent receipts and any legacy push tokens.
    # Public schedules/news are shared university records, not this user's records.
    return Response(status_code=204)
