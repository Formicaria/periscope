from periscope_github.cogs.commands import commit_line, issue_line, language_bar, pages_from_lines, repo_line, run_line
from periscope_github.cogs.poller import adapt
from periscope_github.render import (
    GREEN, GREY, RED, ci_transition, is_bot_sender, one_liner, render, short_ref,
)

LAB = "THE LAB"
REPO = {"name": "anthill", "full_name": "formicaria/anthill", "html_url": "https://github.com/formicaria/anthill",
        "default_branch": "main"}
SENDER = {"login": "alice", "avatar_url": "https://avatars.githubusercontent.com/u/1", "html_url": "https://github.com/alice"}


def base(**kw):
    p = {"repository": REPO, "sender": SENDER}
    p.update(kw)
    return p


def test_push():
    commits = [{"id": f"{i:040x}", "message": f"commit {i}\n\nbody", "author": {"name": "alice"},
                "url": f"https://github.com/formicaria/anthill/commit/{i}"} for i in range(7)]
    e = render("push", base(ref="refs/heads/main", commits=commits, compare="https://x/compare", forced=True), LAB)
    assert e is not None
    assert "7 new commits" in e.title and "force-push" in e.title
    assert e.url == "https://x/compare"
    assert e.description.count("\n") == 5  # 5 commits + "and 2 more"
    assert "and 2 more" in e.description
    assert e.author.name == "alice"
    assert "THE LAB" in e.footer.text
    assert render("push", base(ref="refs/heads/x", deleted=True, commits=[]), LAB) is None
    assert render("push", base(ref="refs/heads/x", commits=[]), LAB) is None


def test_create_delete():
    e = render("create", base(ref_type="tag", ref="v1.0"), LAB)
    assert "tag created: v1.0" in e.title
    e = render("delete", base(ref_type="branch", ref="feature"), LAB)
    assert "branch deleted: feature" in e.title
    assert short_ref("refs/heads/main") == "main" and short_ref("refs/tags/v1") == "v1"


def test_pull_request_colors():
    pr = {"number": 7, "title": "Add thing", "html_url": "https://pr", "base": {"ref": "main"},
          "head": {"ref": "feat", "label": "alice:feat"}, "additions": 10, "deletions": 2, "changed_files": 3,
          "draft": False, "merged": True}
    e = render("pull_request", base(action="closed", pull_request=pr), LAB)
    assert "merged" in e.title and e.color.value == GREEN
    assert "+10/-2" in e.description and "3 files" in e.description and "`main` ← `alice:feat`" in e.description
    pr["merged"] = False
    e = render("pull_request", base(action="closed", pull_request=pr), LAB)
    assert "closed" in e.title and e.color.value == RED
    pr["draft"] = True
    e = render("pull_request", base(action="opened", pull_request=pr), LAB)
    assert "(draft)" in e.title and e.color.value == GREY
    assert render("pull_request", base(action="synchronize", pull_request=pr), LAB) is None
    e = render("pull_request", base(action="review_requested", pull_request=pr, requested_reviewer={"login": "bob"}), LAB)
    assert "review requested from bob" in e.title


def test_review_issue_comment_release():
    pr = {"number": 1, "title": "T", "html_url": "https://pr"}
    e = render("pull_request_review", base(action="submitted", review={"state": "approved", "body": "lgtm"}, pull_request=pr), LAB)
    assert "approved" in e.title and e.color.value == GREEN
    assert render("pull_request_review", base(action="edited", review={}, pull_request=pr), LAB) is None

    issue = {"number": 3, "title": "Bug", "html_url": "https://i", "labels": [{"name": "bug"}], "assignees": [{"login": "bob"}],
             "body": "x" * 500}
    e = render("issues", base(action="opened", issue=issue), LAB)
    assert "Issue #3 opened: Bug" in e.title and len(e.description) == 300
    assert any(f.name == "Labels" and "`bug`" in f.value for f in e.fields)
    e = render("issues", base(action="labeled", issue=issue, label={"name": "urgent"}), LAB)
    assert "labeled `urgent`" in e.title

    e = render("issue_comment", base(action="created", issue=issue, comment={"body": "y" * 1000, "html_url": "https://c"}), LAB)
    assert "comment on Issue #3" in e.title and "[view comment](https://c)" in e.description
    assert len(e.description) < 400

    rel = {"name": "v2", "tag_name": "v2.0.0", "html_url": "https://r", "body": "z" * 900, "assets": [1, 2]}
    e = render("release", base(action="published", release=rel), LAB)
    assert "Release v2" in e.title and len(e.description) == 500
    assert [f.value for f in e.fields] == ["`v2.0.0`", "2"]
    assert render("release", base(action="created", release=rel), LAB) is None


def test_workflow_run_and_ci_transition():
    run = {"name": "CI", "conclusion": "failure", "head_branch": "main", "html_url": "https://run", "run_number": 9,
           "head_sha": "abcdef1234", "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:02:30Z",
           "display_title": "fix", "actor": {"login": "alice"}}
    p = base(action="completed", workflow_run=run)
    e = render("workflow_run", p, LAB)
    assert "❌ CI failure on main" in e.title and "2m" in e.description and e.color.value == RED
    kind, fp, info = ci_transition(p)
    assert kind == "fire" and fp == "gh:anthill:workflow:CI:main" and info["sha"] == "abcdef1"
    run["conclusion"] = "success"
    assert ci_transition(p)[0] == "resolve"
    run["head_branch"] = "feature"
    assert ci_transition(p) is None
    assert render("workflow_run", base(action="requested", workflow_run=run), LAB) is None


def test_misc_events():
    assert "starred by alice" in render("star", base(action="created"), LAB).title
    assert "forked to bob/anthill" in render("fork", base(forkee={"full_name": "bob/anthill", "html_url": "https://f"}), LAB).title
    e = render("repository", base(action="renamed", changes={"repository": {"name": {"from": "old"}}}), LAB)
    assert "Repository renamed" in e.title and "old" in e.description
    assert render("repository", base(action="edited"), LAB) is None
    e = render("member", base(action="added", member={"login": "bob", "html_url": "https://b"}), LAB)
    assert "bob added to formicaria/anthill" in e.title
    e = render("membership", {"action": "removed", "member": {"login": "bob"}, "team": {"name": "core"}, "sender": SENDER}, LAB)
    assert "bob removed from team core" in e.title
    e = render("deployment_status", base(deployment_status={"state": "success", "environment": "prod"}, deployment={"ref": "main"}), LAB)
    assert "deployment to prod: success" in e.title
    assert render("deployment_status", base(deployment_status={"state": "pending"}, deployment={}), LAB) is None
    e = render("discussion", base(action="created", discussion={"title": "Q?", "html_url": "https://d", "category": {"name": "Ideas"}}), LAB)
    assert "discussion created in Ideas: Q?" in e.title
    e = render("discussion_comment", base(action="created", discussion={"title": "Q?"}, comment={"body": "a", "html_url": "https://dc"}), LAB)
    assert "comment on discussion" in e.title
    assert render("check_run", base(action="completed", check_run={"conclusion": "success", "name": "lint"}), LAB) is None
    e = render("check_run", base(action="completed", check_run={"conclusion": "failure", "name": "lint", "head_sha": "1234567890", "output": {"title": "3 errors"}}), LAB)
    assert "check failed: lint" in e.title and "3 errors" in e.description
    assert render("unknown_event", base(), LAB) is None


def test_bot_sender_and_one_liner():
    assert is_bot_sender({"sender": {"login": "dependabot[bot]"}})
    assert is_bot_sender({"sender": {"login": "x", "type": "Bot"}})
    assert not is_bot_sender(base())
    e = render("star", base(action="created"), LAB)
    line = one_liner("star", e)
    assert line.startswith("<t:") and "starred by alice" in line and "](https://github.com/formicaria/anthill)" in line


def test_poll_adapters():
    ev = {"id": "123", "type": "PushEvent", "created_at": "2026-01-01T00:00:00Z",
          "actor": {"login": "alice", "avatar_url": "https://a"}, "repo": {"name": "formicaria/anthill"},
          "payload": {"ref": "refs/heads/main", "before": "a" * 40, "head": "b" * 40,
                      "commits": [{"sha": "c" * 40, "message": "hi", "author": {"name": "alice"}}]}}
    name, payload = adapt(ev)
    assert name == "push"
    assert payload["repository"]["name"] == "anthill" and payload["sender"]["login"] == "alice"
    assert payload["commits"][0]["url"].endswith("/commit/" + "c" * 40)
    assert payload["compare"] == f"https://github.com/formicaria/anthill/compare/{'a' * 12}...{'b' * 12}"
    e = render(name, payload, LAB)
    assert "1 new commit" in e.title and "`ccccccc`" in e.description

    ev = {"type": "WatchEvent", "actor": {"login": "bob"}, "repo": {"name": "formicaria/anthill"}, "payload": {"action": "started"}}
    name, payload = adapt(ev)
    assert name == "star" and "starred by bob" in render(name, payload, LAB).title

    ev = {"type": "CreateEvent", "actor": {"login": "bob"}, "repo": {"name": "formicaria/new"},
          "payload": {"ref_type": "repository", "master_branch": "main"}}
    name, payload = adapt(ev)
    assert name == "repository" and payload["action"] == "created"

    ev = {"type": "PullRequestEvent", "actor": {"login": "bob"}, "repo": {"name": "formicaria/anthill"},
          "payload": {"action": "opened", "number": 5, "pull_request": {"number": 5, "title": "x", "base": {"ref": "main"}, "head": {"ref": "f"}}}}
    name, payload = adapt(ev)
    assert "PR #5 opened: x" in render(name, payload, LAB).title
    assert adapt({"type": "GollumEvent"}) is None


def test_command_formatting():
    r = {"name": "anthill", "html_url": "https://r", "stargazers_count": 3, "open_issues_count": 1,
         "pushed_at": "2026-01-01T00:00:00Z", "private": True}
    assert "🔒" in repo_line(r) and "⭐ 3" in repo_line(r) and "<t:" in repo_line(r)
    it = {"repository_url": "https://api.github.com/repos/formicaria/anthill", "number": 2, "title": "T",
          "html_url": "https://i", "user": {"login": "bob"}, "updated_at": "2026-01-01T00:00:00Z", "draft": True}
    assert issue_line(it).startswith("`anthill` [#2 T](https://i) (draft)")
    assert run_line({"name": "CI", "run_number": 1, "html_url": "https://x", "head_branch": "main", "conclusion": "success"}).startswith("✅")
    c = {"sha": "abcdef1234", "html_url": "https://c", "commit": {"message": "m\nbody", "author": {"name": "n", "date": "2026-01-01T00:00:00Z"}}}
    assert commit_line(c).startswith("[`abcdef1`](https://c) m — n")
    assert language_bar({"Python": 75, "Shell": 25}) == "Python 75%, Shell 25%"
    pages = pages_from_lines("t", [str(i) for i in range(25)], LAB, per_page=10)
    assert len(pages) == 3 and pages[0].title == "t (1/3)" and pages[2].description == "20\n21\n22\n23\n24"
    assert pages_from_lines("t", [], LAB)[0].description == "Nothing to show."


def test_verbose_mode_shows_everything():
    pr = {"number": 1, "title": "T", "html_url": "u", "base": {"ref": "main"}, "head": {"ref": "f", "sha": "abcdef12345"}}
    assert render("pull_request", base(action="synchronize", pull_request=pr), LAB) is None
    e = render("pull_request", base(action="synchronize", pull_request=pr), LAB, verbose=True)
    assert e is not None and "updated (abcdef1)" in e.title
    e = render("pull_request", base(action="labeled", pull_request=pr), LAB, verbose=True)
    assert e is not None and "labeled" in e.title
    run = {"name": "ci", "head_branch": "main", "run_number": 2, "head_sha": "abc1234", "html_url": "r"}
    assert render("workflow_run", base(action="in_progress", workflow_run=run), LAB) is None
    e = render("workflow_run", base(action="in_progress", workflow_run=run), LAB, verbose=True)
    assert e is not None and "▶️" in e.title and "in progress" in e.title
    # unknown event → generic card, only when the payload names a repository
    assert render("gollum", base(action="edited", pages=[]), LAB) is None
    e = render("gollum", base(action="edited", pages=[]), LAB, verbose=True)
    assert e is not None and "gollum edited" in e.title
    assert render("gollum", {"action": "edited"}, LAB, verbose=True) is None
