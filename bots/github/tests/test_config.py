import pytest

from periscope_github.config import GithubSettings, parse_channel_map


def test_defaults(monkeypatch):
    for k in ("GITHUB_ORG", "GITHUB_TOKEN", "GITHUB_REPO_CHANNEL_MAP", "GITHUB_EVENTS", "GITHUB_POLL_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = GithubSettings.from_env()
    assert s.org == "formicaria"
    assert s.ignore_bots is True
    assert s.poll_enabled is False
    assert s.poll_interval_s == 120
    assert s.wants_event("push")


def test_full_env(monkeypatch):
    monkeypatch.setenv("GITHUB_ORG", "acme")
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_x")
    monkeypatch.setenv("GITHUB_FEED_CHANNEL_ID", "100")
    monkeypatch.setenv("GITHUB_REPO_CHANNEL_MAP", "anthill=123, sovrgn=456,op-*=789")
    monkeypatch.setenv("GITHUB_EVENTS", "push,Pull_Request")
    monkeypatch.setenv("GITHUB_IGNORE_BOTS", "false")
    monkeypatch.setenv("GITHUB_CI_FAILURE_ROLE_ID", "555")
    monkeypatch.setenv("GITHUB_POLL_ENABLED", "true")
    monkeypatch.setenv("GITHUB_POLL_INTERVAL_S", "90")
    s = GithubSettings.from_env()
    assert s.org == "acme"
    assert s.repo_channel_map == {"anthill": 123, "sovrgn": 456, "op-*": 789}
    assert s.events == ["push", "pull_request"]
    assert s.wants_event("PUSH") and not s.wants_event("issues")
    assert s.ignore_bots is False
    assert s.ci_failure_role_id == 555
    assert s.poll_enabled and s.poll_interval_s == 90


def test_channel_routing():
    s = GithubSettings(feed_channel_id=1, repo_channel_map={"anthill": 123, "op-*": 789})
    assert s.channel_for("anthill", None) == 123
    assert s.channel_for("op-microround", None) == 789
    assert s.channel_for("other", None) == 1
    assert GithubSettings().channel_for("other", 42) == 42
    assert GithubSettings().channel_for("other", None) is None


def test_bad_channel_map():
    with pytest.raises(RuntimeError):
        parse_channel_map(["anthill"])
    with pytest.raises(RuntimeError):
        parse_channel_map(["anthill=abc"])


def test_poll_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_POLL_ENABLED", "true")
    with pytest.raises(RuntimeError):
        GithubSettings.from_env()
