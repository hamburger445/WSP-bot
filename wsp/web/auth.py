"""Discord OAuth and session helpers for the command-center dashboard."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request

from wsp.constants import PermissionLevel

DISCORD_API = "https://discord.com/api/v10"
SCOPES = "identify guilds guilds.members.read"


def login_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "prompt": "consent",
        }
    )
    return f"https://discord.com/api/oauth2/authorize?{query}"


async def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()


async def fetch_user(access_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


async def fetch_member(access_token: str, guild_id: int, user_id: int) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{DISCORD_API}/users/@me/guilds/{guild_id}/member",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


def session_user(request: Request) -> dict[str, Any] | None:
    return request.session.get("user")


def require_level(user: dict[str, Any] | None, minimum: PermissionLevel) -> bool:
    if not user:
        return False
    return int(user.get("level", 0)) >= int(minimum)
