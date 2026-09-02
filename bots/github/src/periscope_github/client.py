"""Read-only GitHub REST client built on periscope.HttpClient."""

from __future__ import annotations

import logging
from typing import Any

from periscope import Alert, HttpClient, Severity

from .config import GithubSettings

log = logging.getLogger(__name__)


class GithubClient(HttpClient):
    def __init__(self, cfg: GithubSettings):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "periscope-github",
        }
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        super().__init__(cfg.api_url, headers=headers, timeout_s=20)
        self.org = cfg.org

    # ----- org / repos ------------------------------------------------------

    async def list_repos(self) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while page <= 10:
            chunk = await self.get_json(f"/orgs/{self.org}/repos",
                                        params={"per_page": 100, "page": page, "sort": "pushed"})
            repos.extend(chunk)
            if len(chunk) < 100:
                break
            page += 1
        return repos

    async def repo(self, name: str) -> dict[str, Any]:
        return await self.get_json(f"/repos/{self.org}/{name}")

    async def languages(self, name: str) -> dict[str, int]:
        return await self.get_json(f"/repos/{self.org}/{name}/languages")

    async def latest_release(self, name: str) -> dict[str, Any] | None:
        from periscope.http import HttpError
        try:
            return await self.get_json(f"/repos/{self.org}/{name}/releases/latest")
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    # ----- search -------------------------------------------------------------

    async def search_issues(self, query: str, per_page: int = 30) -> dict[str, Any]:
        return await self.get_json("/search/issues",
                                   params={"q": query, "per_page": per_page, "sort": "updated", "order": "desc"})

    async def open_prs(self, repo: str | None = None, per_page: int = 30) -> dict[str, Any]:
        q = f"is:pr is:open org:{self.org}" if not repo else f"is:pr is:open repo:{self.org}/{repo}"
        return await self.search_issues(q, per_page)

    async def open_issues(self, repo: str | None = None, per_page: int = 30) -> dict[str, Any]:
        q = f"is:issue is:open org:{self.org}" if not repo else f"is:issue is:open repo:{self.org}/{repo}"
        return await self.search_issues(q, per_page)

    # ----- actions / commits --------------------------------------------------

    async def workflow_runs(self, repo: str, per_page: int = 10, branch: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        data = await self.get_json(f"/repos/{self.org}/{repo}/actions/runs", params=params)
        return data.get("workflow_runs", [])

    async def workflow_run(self, repo: str, run_id: int) -> dict[str, Any]:
        return await self.get_json(f"/repos/{self.org}/{repo}/actions/runs/{run_id}")

    async def workflow_run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        data = await self.get_json(f"/repos/{self.org}/{repo}/actions/runs/{run_id}/jobs", params={"per_page": 100})
        return data.get("jobs", [])

    async def commits(self, repo: str, branch: str | None = None, n: int = 10) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": max(1, min(n, 30))}
        if branch:
            params["sha"] = branch
        return await self.get_json(f"/repos/{self.org}/{repo}/commits", params=params)

    # ----- polling ------------------------------------------------------------

    async def org_events(self, etag: str | None) -> tuple[int, list[dict[str, Any]], str | None, int]:
        """Return (status, events, etag, poll_interval_s). status 304 => nothing new (events empty)."""
        headers = {"If-None-Match": etag} if etag else {}
        resp = await self.request("GET", f"/orgs/{self.org}/events", headers=headers, params={"per_page": 100})
        async with resp:
            interval = int(resp.headers.get("X-Poll-Interval", "60") or 60)
            new_etag = resp.headers.get("ETag")
            if resp.status == 304:
                return 304, [], etag, interval
            return resp.status, await resp.json(content_type=None), new_etag, interval


class Reachability:
    """Counts consecutive API failures; fires a CRITICAL alert after 3 and resolves on recovery."""

    FINGERPRINT = "gh:api:unreachable"

    def __init__(self, bot, service: str = "GitHub API"):
        self.bot = bot
        self.service = service
        self.failures = 0

    async def failure(self, err: BaseException) -> None:
        self.failures += 1
        log.warning("%s call failed (%d consecutive): %s", self.service, self.failures, err)
        if self.failures == 3:
            await self.bot.alerts.fire(Alert(
                fingerprint=self.FINGERPRINT,
                title=f"{self.service} unreachable",
                description=f"3 consecutive failures. Last error: `{str(err)[:300]}`",
                severity=Severity.CRITICAL,
            ))

    async def success(self) -> None:
        if self.failures >= 3:
            await self.bot.alerts.resolve(self.FINGERPRINT, note="API reachable again")
        self.failures = 0
