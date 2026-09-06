"""Discord OAuth and session helpers for the command-center dashboard."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request

from wsp.constants import PermissionLevel

DISCORD_API = "https://discord.com/api/v10"
SCOPES = "identify guilds guilds.members.read"


class OAuthRateLimited(httpx.HTTPStatusError):
    """Discord rejected an OAuth request temporarily."""

    def __init__(self, response: httpx.Response, retry_after: float) -> None:
        super().__init__("Discord OAuth rate limit", request=response.request, response=response)
        self.retry_after = retry_after


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
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        for attempt in range(2):
            response = await client.post(
                f"{DISCORD_API}/oauth2/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response.json()
            try:
                retry_after = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                retry_after = 1.0
            if attempt == 1:
                raise OAuthRateLimited(response, retry_after)
            await asyncio.sleep(min(max(retry_after, 1.0), 10.0))
        raise RuntimeError("OAuth exchange did not return a response")


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
