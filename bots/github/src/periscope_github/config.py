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
    ci_channel_id: int | None = None
    repo_channel_map: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)  # empty = all
    ignore_bots: bool = True
    ci_failure_role_id: int | None = None
    poll_enabled: bool = True
    poll_interval_s: int = 120
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls) -> "GithubSettings":
        s = cls(
            org=env("GITHUB_ORG", "formicaria"),
            token=env("GITHUB_TOKEN"),
            feed_channel_id=env_int("GITHUB_FEED_CHANNEL_ID"),
            ci_channel_id=env_int("GITHUB_CI_CHANNEL_ID"),
            repo_channel_map=parse_channel_map(env_list("GITHUB_REPO_CHANNEL_MAP")),
            events=[e.lower() for e in env_list("GITHUB_EVENTS")],
            ignore_bots=env_bool("GITHUB_IGNORE_BOTS", True),
            ci_failure_role_id=env_int("GITHUB_CI_FAILURE_ROLE_ID"),
            poll_enabled=env_bool("GITHUB_POLL_ENABLED", True),
            poll_interval_s=env_int("GITHUB_POLL_INTERVAL_S", 120),
            api_url=env("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
        )
        if s.poll_enabled and not s.token:
            s.poll_enabled = False  # webhook-only without a PAT
        if s.poll_interval_s < 30:
            raise RuntimeError("GITHUB_POLL_INTERVAL_S must be >= 30 (GitHub enforces X-Poll-Interval)")
        return s

    CI_EVENTS = ("workflow_run", "check_run", "check_suite", "workflow_job")

    def channels_for(self, repo_name: str, event: str, default: int | None) -> list[int]:
        """Every channel an event should be posted to.

        CI events go to GITHUB_CI_CHANNEL_ID (else the feed). Everything else goes to the feed channel,
        plus a per-repo channel from GITHUB_REPO_CHANNEL_MAP when one matches (exact name, then glob).
        """
        feed = self.feed_channel_id or default
        if event in self.CI_EVENTS:
            target = self.ci_channel_id or feed
            return [target] if target else []
        out: list[int] = [feed] if feed else []
        mapped = self.repo_channel_map.get(repo_name)
        if mapped is None:
            for pattern, cid in self.repo_channel_map.items():
                if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(repo_name, pattern):
                    mapped = cid
                    break
        if mapped and mapped not in out:
            out.append(mapped)
        return out

    def channel_for(self, repo_name: str, default: int | None) -> int | None:
        """First channel for a repo's non-CI events (kept for callers that want one)."""
        chans = self.channels_for(repo_name, "push", default)
        return chans[0] if chans else None

    def wants_event(self, event: str) -> bool:
        return not self.events or event.lower() in self.events
