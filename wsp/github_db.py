"""Sync the SQLite database to a GitHub branch so data survives deploys.

The live file stays local (or on a Render disk). Snapshots are stored on the
`data` branch as `data/wsp.db` so pushes do not retrigger a main-branch deploy.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from wsp.config import Settings
    from wsp.db import Database

log = logging.getLogger("wsp.github_db")

API = "https://api.github.com"
HEADERS_JSON = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "WSP-bot",
}


class GitHubDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._last_sha: str | None = None
        self._debounced: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.github_token and self.settings.github_repo)

    def _headers(self) -> dict[str, str]:
        return {**HEADERS_JSON, "Authorization": f"Bearer {self.settings.github_token}"}

    def _contents_url(self) -> str:
        return f"{API}/repos/{self.settings.github_repo}/contents/{self.settings.github_db_path}"

    async def restore(self, dest: Path) -> bool:
        """Download the GitHub snapshot if the local database is missing or empty."""
        if not self.enabled:
            log.info("GitHub database sync is off (set GITHUB_TOKEN to enable it).")
            return False
        if dest.exists() and dest.stat().st_size > 8192:
            log.info("Local database already has data at %s; keeping it and using GitHub as backup.", dest)
            return False
        async with self._lock:
            payload = await self._get_file()
            if payload is None:
                log.info("No database on GitHub yet; a snapshot will be pushed after the bot has data.")
                return False
            raw = await self._download_bytes(payload)
            if not raw:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".download")
            tmp.write_bytes(raw)
            tmp.replace(dest)
            self._last_sha = payload.get("sha")
            log.info("Restored database from GitHub (%s bytes) to %s", len(raw), dest)
            return True

    async def push(self, db: Database) -> bool:
        if not self.enabled:
            return False
        async with self._lock:
            try:
                snapshot = await db.snapshot_bytes()
            except Exception:
                log.exception("Could not snapshot the database for GitHub")
                return False
            if not snapshot:
                return False
            await self._ensure_branch()
            remote = await self._get_file()
            remote_sha = remote.get("sha") if remote else None
            if remote:
                remote_bytes = await self._download_bytes(remote)
                if remote_bytes and hashlib.sha256(remote_bytes).digest() == hashlib.sha256(snapshot).digest():
                    log.debug("GitHub database already matches local snapshot")
                    return True
            body = {
                "message": "Sync WSP database (shifts, setup, personnel)",
                "content": base64.b64encode(snapshot).decode("ascii"),
                "branch": self.settings.github_db_branch,
            }
            if remote_sha:
                body["sha"] = remote_sha
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.put(self._contents_url(), headers=self._headers(), json=body)
            if response.status_code in {200, 201}:
                data = response.json()
                self._last_sha = (data.get("content") or {}).get("sha") or remote_sha
                log.info("Pushed database to GitHub %s@%s (%s bytes)", self.settings.github_repo, self.settings.github_db_branch, len(snapshot))
                return True
            log.error("GitHub database push failed (%s): %s", response.status_code, response.text[:500])
            return False

    def schedule_push(self, db: Database) -> None:
        if not self.enabled:
            return
        if self._debounced and not self._debounced.done():
            self._debounced.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(8)
                await self.push(db)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Debounced GitHub push failed")

        self._debounced = asyncio.create_task(_run())

    async def _get_file(self) -> dict | None:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                self._contents_url(),
                headers=self._headers(),
                params={"ref": self.settings.github_db_branch},
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            log.warning("GitHub database fetch failed (%s): %s", response.status_code, response.text[:300])
            return None
        return response.json()

    async def _download_bytes(self, payload: dict) -> bytes:
        url = payload.get("download_url")
        if url:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            return response.content
        return _decode_content(payload)

    async def _ensure_branch(self) -> None:
        owner_repo = self.settings.github_repo
        branch = self.settings.github_db_branch
        async with httpx.AsyncClient(timeout=30) as client:
            existing = await client.get(
                f"{API}/repos/{owner_repo}/git/ref/heads/{branch}",
                headers=self._headers(),
            )
            if existing.status_code == 200:
                return
            repo = await client.get(f"{API}/repos/{owner_repo}", headers=self._headers())
            repo.raise_for_status()
            default = repo.json().get("default_branch") or "main"
            head = await client.get(
                f"{API}/repos/{owner_repo}/git/ref/heads/{default}",
                headers=self._headers(),
            )
            head.raise_for_status()
            sha = head.json()["object"]["sha"]
            created = await client.post(
                f"{API}/repos/{owner_repo}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{branch}", "sha": sha},
            )
            if created.status_code in {201, 422}:
                log.info("Using GitHub branch %s for database snapshots", branch)
            else:
                created.raise_for_status()


def _decode_content(payload: dict) -> bytes:
    encoding = payload.get("encoding")
    content = payload.get("content") or ""
    if encoding == "base64":
        return base64.b64decode(content)
    if encoding == "utf-8":
        return str(content).encode("utf-8")
    return b""
