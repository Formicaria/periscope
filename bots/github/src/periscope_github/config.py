"""GitHub-specific configuration (periscope.Settings covers the Discord side)."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from periscope import env, env_bool, env_int, env_list


def parse_channel_map(raw: list[str]) -> dict[str, int]:
    """Parse ["anthill=123", "op-*=456"] into an ordered {pattern: channel_id} dict."""
    out: dict[str, int] = {}
    for item in raw:
        if "=" not in item:
            raise RuntimeError(f"GITHUB_REPO_CHANNEL_MAP entry {item!r} must look like repo=channel_id")
        pattern, _, cid = item.partition("=")
        pattern, cid = pattern.strip(), cid.strip()
        if not pattern or not cid.isdigit():
            raise RuntimeError(f"GITHUB_REPO_CHANNEL_MAP entry {item!r} must look like repo=channel_id")
        out[pattern] = int(cid)
    return out


@dataclass
class GithubSettings:
    org: str = "formicaria"
    token: str | None = None
    feed_channel_id: int | None = None
    repo_channel_map: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)  # empty = all
    ignore_bots: bool = True
    ci_failure_role_id: int | None = None
    poll_enabled: bool = False
    poll_interval_s: int = 120
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> "GithubSettings":
        s = cls(
            org=env("GITHUB_ORG", "formicaria"),
            token=env("GITHUB_TOKEN"),
            feed_channel_id=env_int("GITHUB_FEED_CHANNEL_ID"),
            repo_channel_map=parse_channel_map(env_list("GITHUB_REPO_CHANNEL_MAP")),
            events=[e.lower() for e in env_list("GITHUB_EVENTS")],
            ignore_bots=env_bool("GITHUB_IGNORE_BOTS", True),
            ci_failure_role_id=env_int("GITHUB_CI_FAILURE_ROLE_ID"),
            poll_enabled=env_bool("GITHUB_POLL_ENABLED", False),
            poll_interval_s=env_int("GITHUB_POLL_INTERVAL_S", 120),
            api_url=env("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        )
        if s.poll_enabled and not s.token:
            raise RuntimeError("GITHUB_POLL_ENABLED=true requires GITHUB_TOKEN")
        if s.poll_interval_s < 30:
            raise RuntimeError("GITHUB_POLL_INTERVAL_S must be >= 30 (GitHub enforces X-Poll-Interval)")
        return s

    def channel_for(self, repo_name: str, default: int | None) -> int | None:
        """Pick the Discord channel for a repo: exact match first, then glob patterns, then default."""
        if repo_name in self.repo_channel_map:
            return self.repo_channel_map[repo_name]
        for pattern, cid in self.repo_channel_map.items():
            if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(repo_name, pattern):
                return cid
        return self.feed_channel_id or default

    def wants_event(self, event: str) -> bool:
        return not self.events or event.lower() in self.events
