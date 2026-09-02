import pytest

from periscope_github.config import GithubSettings, parse_channel_map


def test_defaults(monkeypatch):
    for k in ("GITHUB_ORG", "GITHUB_TOKEN", "GITHUB_REPO_CHANNEL_MAP", "GITHUB_EVENTS", "GITHUB_POLL_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    s = GithubSettings.from_env()
    assert s.org == "formicaria"
    assert s.ignore_bots is False and s.verbose is True   # everything, everyone, by default
    assert s.poll_enabled is False                         # no token in this test → webhook-only
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
    assert s.channel_for("anthill", None) == 1            # the feed always comes first
    assert s.channels_for("anthill", "push", None) == [1, 123]
    assert s.channels_for("op-microround", "push", None) == [1, 789]
    assert s.channels_for("other", "push", None) == [1]
    assert GithubSettings().channel_for("other", 42) == 42
    assert GithubSettings().channel_for("other", None) is None


def test_bad_channel_map():
    with pytest.raises(RuntimeError):
        parse_channel_map(["anthill"])
    with pytest.raises(RuntimeError):
        parse_channel_map(["anthill=abc"])


def test_poll_needs_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_POLL_ENABLED", "true")
    assert GithubSettings.from_env().poll_enabled is False   # silently webhook-only without a PAT
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("GITHUB_POLL_ENABLED", raising=False)
    assert GithubSettings.from_env().poll_enabled is True    # default on


def test_channels_for_feed_gets_everything_and_ci_goes_to_ci_channel():
    from periscope_github.config import GithubSettings
    s = GithubSettings(feed_channel_id=1, ci_channel_id=2, repo_channel_map={"Anthill": 10, "op-*": 20})
    assert s.channels_for("Anthill", "push", None) == [1, 10]        # feed + mirror
    assert s.channels_for("op-thing", "issues", None) == [1, 20]     # glob mirror
    assert s.channels_for("other", "push", None) == [1]
    assert s.channels_for("Anthill", "workflow_run", None) == [2]    # CI never mirrors
    s2 = GithubSettings(feed_channel_id=1)
    assert s2.channels_for("x", "workflow_run", None) == [1]         # CI falls back to feed
    assert GithubSettings().channels_for("x", "push", 99) == [99]    # alert channel as last resort


def test_adapt_workflow_run_shapes_a_webhook_payload():
    from periscope_github.cogs.poller import adapt_workflow_run
    run = {"id": 7, "name": "ci", "status": "completed", "conclusion": "failure", "head_branch": "main",
           "head_sha": "abcdef1234", "html_url": "https://github.com/Formicaria/periscope/actions/runs/7",
           "run_number": 3, "triggering_actor": {"login": "xchronusx"},
           "repository": {"full_name": "Formicaria/periscope", "default_branch": "main"}}
    p = adapt_workflow_run("periscope", run, "Formicaria")
    assert p["action"] == "completed" and p["workflow_run"] is run
    assert p["repository"]["name"] == "periscope" and p["repository"]["full_name"] == "Formicaria/periscope"
    assert p["sender"]["login"] == "xchronusx"
    from periscope_github.render import render
    e = render("workflow_run", p, "Formicaria")
    assert e is not None and "❌" in e.title and "main" in e.title
