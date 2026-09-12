"""SQLite snapshot stored as a file on a GitHub branch."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from wsp.config import Settings

if TYPE_CHECKING:
    from wsp.db import Database

log = logging.getLogger("wsp.github_db")
API = "https://api.github.com"


class GitHubDatabase:
    def __init__(self, token: str, repo: str, branch: str, remote_path: str) -> None:
        self.token = token
        self.repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git")
        self.branch = branch or "data"
        self.remote_path = remote_path.strip().lstrip("/") or "data/wsp.db"
        self._sha: str | None = None
        self._push_task: asyncio.Task | None = None
        self._db: Database | None = None
        self._debounce = 4.0

    @classmethod
    def from_settings(cls, settings: Settings) -> GitHubDatabase | None:
        if not settings.github_token or not settings.github_repo:
            log.info("GitHub database sync is off (set GITHUB_TOKEN and GITHUB_REPO)")
            return None
        return cls(
            settings.github_token,
            settings.github_repo,
            settings.github_db_branch,
            settings.github_db_path,
        )

    def bind(self, db: Database) -> None:
        self._db = db
        db.on_change = self.schedule_push

    def schedule_push(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
        self._push_task = loop.create_task(self._debounced_push(), name="wsp-github-db-push")

    async def _debounced_push(self) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        await self.push()

    async def restore(self, dest: Path) -> bool:
        try:
            payload = await asyncio.to_thread(self._get_file)
        except Exception:
            log.exception("Could not restore database from GitHub")
            return False
        if payload is None:
            log.info("No GitHub database at %s:%s", self.branch, self.remote_path)
            return False
        content = payload.get("content") or ""
        encoding = payload.get("encoding") or "base64"
        if encoding != "base64" or not content:
            log.warning("GitHub database encoding is not base64")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(content.replace("\n", "")))
        for extra in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm"), dest.with_name(dest.name + "-journal")):
            extra.unlink(missing_ok=True)
        self._sha = payload.get("sha")
        log.info("Restored database from GitHub (%s bytes, sha=%s)", dest.stat().st_size, self._sha)
        return True

    async def push(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            data = await db.snapshot_bytes()
            if not data:
                return
            await asyncio.to_thread(self._put_file, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not push database to GitHub")

    async def flush(self) -> None:
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        await self.push()

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "wsp-bot",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, url: str, body: dict | None = None) -> dict | None:
        data = None if body is None else json.dumps(body).encode()
        req = Request(url, data=data, headers=self._headers(), method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else {}
        except HTTPError as exc:
            err = exc.read().decode(errors="replace")
            if exc.code == 404:
                return None
            raise RuntimeError(f"GitHub API {method} {url} failed ({exc.code}): {err}") from exc

    def _encoded_path(self) -> str:
        return quote(self.remote_path)

    def _get_file(self) -> dict | None:
        url = f"{API}/repos/{self.repo}/contents/{self._encoded_path()}?ref={quote(self.branch)}"
        payload = self._request("GET", url)
        if payload:
            self._sha = payload.get("sha")
        return payload

    def _ensure_branch(self) -> None:
        ref = self._request("GET", f"{API}/repos/{self.repo}/git/ref/heads/{quote(self.branch)}")
        if ref is not None:
            return
        repo = self._request("GET", f"{API}/repos/{self.repo}")
        if not repo:
            raise RuntimeError(f"GitHub repo {self.repo} was not found")
        default_branch = repo.get("default_branch") or "main"
        head = self._request("GET", f"{API}/repos/{self.repo}/git/ref/heads/{quote(default_branch)}")
        if not head or not head.get("object", {}).get("sha"):
            raise RuntimeError(f"Could not read default branch {default_branch}")
        created = self._request(
            "POST",
            f"{API}/repos/{self.repo}/git/refs",
            {"ref": f"refs/heads/{self.branch}", "sha": head["object"]["sha"]},
        )
        if created is None:
            raise RuntimeError(f"Could not create GitHub branch {self.branch}")
        log.info("Created GitHub branch %s", self.branch)

    def _put_file(self, data: bytes) -> None:
        self._ensure_branch()
        if not self._sha:
            current = self._get_file()
            if current:
                self._sha = current.get("sha")
        body = {
            "message": "Update WSP database",
            "content": base64.b64encode(data).decode(),
            "branch": self.branch,
        }
        if self._sha:
            body["sha"] = self._sha
        url = f"{API}/repos/{self.repo}/contents/{self._encoded_path()}"
        result = self._request("PUT", url, body)
        if not result:
            raise RuntimeError("GitHub did not accept the database upload")
        content = result.get("content") or {}
        self._sha = content.get("sha") or self._sha
        log.info("Pushed database to GitHub %s:%s", self.branch, self.remote_path)
