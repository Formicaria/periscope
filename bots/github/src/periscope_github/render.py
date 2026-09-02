"""Pure functions: GitHub webhook payload -> discord.Embed (or None to skip). No network, no bot."""

from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import discord
from periscope import Severity, human_duration, lab_embed, truncate

GREEN, RED, PURPLE, GREY, BLUE, YELLOW = 0x2ECC71, 0xE74C3C, 0x8957E5, 0x95A5A6, 0x3498DB, 0xF1C40F

Renderer = Callable[[dict[str, Any], str], "discord.Embed | None"]


# ----- helpers ---------------------------------------------------------------

def repo_name(p: dict[str, Any]) -> str:
    return (p.get("repository") or {}).get("name") or "?"


def repo_full(p: dict[str, Any]) -> str:
    return (p.get("repository") or {}).get("full_name") or repo_name(p)


def repo_url(p: dict[str, Any]) -> str | None:
    return (p.get("repository") or {}).get("html_url")


def default_branch(p: dict[str, Any]) -> str:
    return (p.get("repository") or {}).get("default_branch") or "main"


def sender_login(p: dict[str, Any]) -> str:
    return (p.get("sender") or {}).get("login") or "unknown"


def is_bot_sender(p: dict[str, Any]) -> bool:
    s = p.get("sender") or {}
    return s.get("type") == "Bot" or sender_login(p).endswith("[bot]")


def short_ref(ref: str | None) -> str:
    if not ref:
        return "?"
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def base_embed(p: dict[str, Any], title: str, description: str | None, lab_name: str, *,
               color: int = BLUE, url: str | None = None) -> discord.Embed:
    e = lab_embed(truncate(title, 256), truncate(description, 4096) if description else None,
                  lab_name=lab_name, color=color, url=url)
    s = p.get("sender") or {}
    if s.get("login"):
        e.set_author(name=s["login"], url=s.get("html_url"), icon_url=s.get("avatar_url"))
    return e


def _labels(obj: dict[str, Any]) -> str:
    return ", ".join(f"`{l.get('name')}`" for l in obj.get("labels") or []) or "—"


def _assignees(obj: dict[str, Any]) -> str:
    return ", ".join(a.get("login", "?") for a in obj.get("assignees") or []) or "—"


# ----- renderers -------------------------------------------------------------

def render_push(p: dict[str, Any], lab: str) -> discord.Embed | None:
    if p.get("deleted"):
        return None
    commits = p.get("commits") or []
    if not commits and not p.get("forced"):
        return None
    branch = short_ref(p.get("ref"))
    n = len(commits)
    title = f"[{repo_name(p)}:{branch}] {n} new commit{'s' if n != 1 else ''}"
    if p.get("forced"):
        title += " ⚠️ force-push"
    lines = []
    for c in commits[:5]:
        sha = (c.get("id") or c.get("sha") or "")[:7]
        msg = (c.get("message") or "").splitlines()[0] if c.get("message") else "(no message)"
        author = (c.get("author") or {}).get("name") or (c.get("author") or {}).get("login") or "?"
        lines.append(f"[`{sha}`]({c.get('url')}) {truncate(msg, 80)} — {author}")
    if n > 5:
        lines.append(f"… and {n - 5} more")
    return base_embed(p, title, "\n".join(lines), lab, color=YELLOW if p.get("forced") else BLUE,
                      url=p.get("compare"))


def render_create(p: dict[str, Any], lab: str) -> discord.Embed | None:
    kind, ref = p.get("ref_type", "ref"), p.get("ref", "?")
    url = f"{repo_url(p)}/tree/{ref}" if repo_url(p) else None
    return base_embed(p, f"[{repo_name(p)}] {kind} created: {ref}", None, lab, color=GREEN, url=url)


def render_delete(p: dict[str, Any], lab: str) -> discord.Embed | None:
    return base_embed(p, f"[{repo_name(p)}] {p.get('ref_type', 'ref')} deleted: {p.get('ref', '?')}", None, lab,
                      color=GREY, url=repo_url(p))


_PR_ACTIONS = {"opened", "closed", "reopened", "ready_for_review", "review_requested", "converted_to_draft"}


def render_pull_request(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    action = p.get("action")
    if action not in _PR_ACTIONS and not verbose:
        return None
    pr = p.get("pull_request") or {}
    merged = bool(pr.get("merged"))
    if action == "closed":
        verb, color = ("merged", GREEN) if merged else ("closed", RED)
    elif action == "review_requested":
        who = (p.get("requested_reviewer") or {}).get("login") or (p.get("requested_team") or {}).get("name") or "?"
        verb, color = f"review requested from {who}", BLUE
    elif action == "ready_for_review":
        verb, color = "ready for review", GREEN
    elif action == "converted_to_draft":
        verb, color = "converted to draft", GREY
    elif action == "synchronize":
        verb, color = f"updated ({(pr.get('head') or {}).get('sha', '')[:7]})", BLUE
    else:
        verb, color = str(action or "?").replace("_", " "), (GREY if pr.get("draft") else BLUE)
    title = f"[{repo_name(p)}] PR #{pr.get('number')} {verb}: {pr.get('title', '')}"
    if pr.get("draft") and action not in ("closed",):
        title += " (draft)"
    base, head = (pr.get("base") or {}).get("ref", "?"), (pr.get("head") or {}).get("label") or (pr.get("head") or {}).get("ref", "?")
    desc = f"`{base}` ← `{head}`"
    stats = []
    if pr.get("additions") is not None:
        stats.append(f"+{pr.get('additions', 0)}/-{pr.get('deletions', 0)}")
    if pr.get("changed_files") is not None:
        stats.append(f"{pr.get('changed_files')} files")
    if stats:
        desc += "  •  " + "  •  ".join(stats)
    if action == "opened" and pr.get("body"):
        desc += "\n\n" + truncate(pr["body"], 300)
    return base_embed(p, title, desc, lab, color=color, url=pr.get("html_url"))


def render_pull_request_review(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    if p.get("action") != "submitted" and not verbose:
        return None
    review, pr = p.get("review") or {}, p.get("pull_request") or {}
    state = (review.get("state") or "commented").lower()
    label, color = {
        "approved": ("✅ approved", GREEN),
        "changes_requested": ("🔁 requested changes on", RED),
    }.get(state, ("💬 commented on", BLUE))
    title = f"[{repo_name(p)}] {label} PR #{pr.get('number')}: {pr.get('title', '')}"
    body = truncate(review.get("body") or "", 300) or None
    return base_embed(p, title, body, lab, color=color, url=review.get("html_url") or pr.get("html_url"))


_ISSUE_ACTIONS = {"opened", "closed", "reopened", "labeled", "unlabeled", "assigned", "unassigned", "pinned"}


def render_issues(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    action = p.get("action")
    if action not in _ISSUE_ACTIONS and not verbose:
        return None
    issue = p.get("issue") or {}
    color = {"opened": GREEN, "reopened": GREEN, "closed": GREY}.get(action, BLUE)
    detail = ""
    if action in ("labeled", "unlabeled"):
        detail = f" `{(p.get('label') or {}).get('name', '?')}`"
    elif action in ("assigned", "unassigned"):
        detail = f" {(p.get('assignee') or {}).get('login', '?')}"
    title = f"[{repo_name(p)}] Issue #{issue.get('number')} {action}{detail}: {issue.get('title', '')}"
    desc = None
    if action == "opened":
        desc = truncate(issue.get("body") or "", 300) or None
    e = base_embed(p, title, desc, lab, color=color, url=issue.get("html_url"))
    if action in ("opened", "labeled", "unlabeled", "assigned", "unassigned"):
        e.add_field(name="Labels", value=_labels(issue), inline=True)
        e.add_field(name="Assignees", value=_assignees(issue), inline=True)
    return e


def render_issue_comment(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    if p.get("action") != "created" and not verbose:
        return None
    issue, comment = p.get("issue") or {}, p.get("comment") or {}
    kind = "PR" if issue.get("pull_request") else "Issue"
    title = f"[{repo_name(p)}] 💬 comment on {kind} #{issue.get('number')}: {issue.get('title', '')}"
    body = truncate(comment.get("body") or "", 300)
    return base_embed(p, title, f"{body}\n[view comment]({comment.get('html_url')})", lab,
                      url=comment.get("html_url"))


def render_release(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    if p.get("action") != "published" and not verbose:
        return None
    r = p.get("release") or {}
    name = r.get("name") or r.get("tag_name") or "release"
    title = f"[{repo_name(p)}] 🚀 Release {name}" + (" (pre-release)" if r.get("prerelease") else "")
    e = base_embed(p, title, truncate(r.get("body") or "", 500) or None, lab, color=PURPLE, url=r.get("html_url"))
    e.add_field(name="Tag", value=f"`{r.get('tag_name', '?')}`", inline=True)
    e.add_field(name="Assets", value=str(len(r.get("assets") or [])), inline=True)
    return e


_CONCLUSION = {
    "success": ("✅", GREEN), "failure": ("❌", RED), "cancelled": ("⏹️", GREY),
    "timed_out": ("⏰", RED), "skipped": ("⏭️", GREY), "neutral": ("⚪", GREY), "action_required": ("⚠️", YELLOW),
}


def render_workflow_run(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    run = p.get("workflow_run") or {}
    if p.get("action") != "completed":
        if not verbose:
            return None
        title = f"[{repo_name(p)}] ▶️ {run.get('name', 'workflow')} {str(p.get('action') or 'started').replace('_', ' ')} on {run.get('head_branch', '?')}"
        desc = f"Run #{run.get('run_number', '?')} • `{(run.get('head_sha') or '')[:7]}` {truncate((run.get('display_title') or ''), 80)}"
        return base_embed(p, title, desc, lab, color=BLUE, url=run.get("html_url"))
    conclusion = run.get("conclusion") or "unknown"
    emoji, color = _CONCLUSION.get(conclusion, ("⚪", GREY))
    started, ended = parse_ts(run.get("run_started_at")), parse_ts(run.get("updated_at"))
    duration = human_duration((ended - started).total_seconds()) if started and ended else "—"
    title = f"[{repo_name(p)}] {emoji} {run.get('name', 'workflow')} {conclusion} on {run.get('head_branch', '?')}"
    desc = (f"Run #{run.get('run_number', '?')} • {duration} • "
            f"`{(run.get('head_sha') or '')[:7]}` {truncate((run.get('display_title') or ''), 80)}")
    return base_embed(p, title, desc, lab, color=color, url=run.get("html_url"))


def render_star(p: dict[str, Any], lab: str) -> discord.Embed | None:
    if p.get("action") not in (None, "created", "started"):
        return None
    stars = (p.get("repository") or {}).get("stargazers_count")
    desc = f"now {stars} ⭐" if stars is not None else None
    return base_embed(p, f"[{repo_name(p)}] ⭐ starred by {sender_login(p)}", desc, lab, color=YELLOW, url=repo_url(p))


def render_fork(p: dict[str, Any], lab: str) -> discord.Embed | None:
    forkee = p.get("forkee") or {}
    return base_embed(p, f"[{repo_name(p)}] 🍴 forked to {forkee.get('full_name', '?')}", None, lab,
                      url=forkee.get("html_url") or repo_url(p))


_REPO_ACTIONS = {"created", "deleted", "archived", "unarchived", "renamed", "publicized", "privatized", "transferred"}


def render_repository(p: dict[str, Any], lab: str) -> discord.Embed | None:
    action = p.get("action")
    if action not in _REPO_ACTIONS:
        return None
    color = {"created": GREEN, "deleted": RED, "archived": GREY, "publicized": YELLOW}.get(action, BLUE)
    title = f"📦 Repository {action}: {repo_full(p)}"
    desc = None
    if action == "renamed":
        old = ((p.get("changes") or {}).get("repository") or {}).get("name", {}).get("from")
        desc = f"was `{old}`" if old else None
    elif action == "created":
        desc = (p.get("repository") or {}).get("description") or None
    return base_embed(p, title, desc, lab, color=color, url=repo_url(p))


def render_member(p: dict[str, Any], lab: str) -> discord.Embed | None:
    action, m = p.get("action"), p.get("member") or {}
    if action not in ("added", "removed"):
        return None
    scope = f"team {(p.get('team') or {}).get('name')}" if p.get("team") else repo_full(p) if p.get("repository") else f"org {(p.get('organization') or {}).get('login', '')}"
    title = f"👤 {m.get('login', '?')} {action} to {scope}" if action == "added" else f"👤 {m.get('login', '?')} removed from {scope}"
    return base_embed(p, title, None, lab, color=GREEN if action == "added" else GREY, url=m.get("html_url"))


def render_organization(p: dict[str, Any], lab: str) -> discord.Embed | None:
    action = p.get("action")
    if action not in ("member_added", "member_removed", "member_invited"):
        return None
    user = (p.get("membership") or {}).get("user") or p.get("invitation") or {}
    org = (p.get("organization") or {}).get("login", "org")
    verb = {"member_added": "joined", "member_removed": "left", "member_invited": "was invited to"}[action]
    return base_embed(p, f"👤 {user.get('login', '?')} {verb} {org}", None, lab,
                      color=GREEN if action != "member_removed" else GREY, url=user.get("html_url"))


def render_deployment_status(p: dict[str, Any], lab: str) -> discord.Embed | None:
    ds, dep = p.get("deployment_status") or {}, p.get("deployment") or {}
    state = ds.get("state") or "unknown"
    if state in ("pending", "queued", "in_progress", "waiting"):
        return None
    emoji, color = {"success": ("✅", GREEN), "failure": ("❌", RED), "error": ("❌", RED),
                    "inactive": ("💤", GREY)}.get(state, ("⚪", GREY))
    env_name = ds.get("environment") or dep.get("environment") or "?"
    title = f"[{repo_name(p)}] {emoji} deployment to {env_name}: {state}"
    desc = f"ref `{dep.get('ref', '?')}`" + (f" — {truncate(ds['description'], 200)}" if ds.get("description") else "")
    return base_embed(p, title, desc, lab, color=color,
                      url=ds.get("target_url") or ds.get("log_url") or ds.get("environment_url"))


def render_discussion(p: dict[str, Any], lab: str) -> discord.Embed | None:
    if p.get("action") not in ("created", "answered", "closed"):
        return None
    d = p.get("discussion") or {}
    cat = (d.get("category") or {}).get("name")
    title = f"[{repo_name(p)}] 🗣️ discussion {p['action']}" + (f" in {cat}" if cat else "") + f": {d.get('title', '')}"
    body = truncate(d.get("body") or "", 300) if p["action"] == "created" else None
    return base_embed(p, title, body or None, lab, url=d.get("html_url"))


def render_discussion_comment(p: dict[str, Any], lab: str) -> discord.Embed | None:
    if p.get("action") != "created":
        return None
    d, c = p.get("discussion") or {}, p.get("comment") or {}
    title = f"[{repo_name(p)}] 💬 comment on discussion: {d.get('title', '')}"
    return base_embed(p, title, f"{truncate(c.get('body') or '', 300)}\n[view comment]({c.get('html_url')})", lab,
                      url=c.get("html_url"))


def render_check_run(p: dict[str, Any], lab: str, verbose: bool = False) -> discord.Embed | None:
    cr = p.get("check_run") or {}
    if p.get("action") != "completed" or (cr.get("conclusion") not in ("failure", "timed_out") and not verbose):
        return None
    out = cr.get("output") or {}
    title = f"[{repo_name(p)}] ❌ check failed: {cr.get('name', '?')}"
    desc = f"`{(cr.get('head_sha') or '')[:7]}`" + (f" — {truncate(out['title'], 200)}" if out.get("title") else "")
    return base_embed(p, title, desc, lab, color=RED, url=cr.get("html_url"))


RENDERERS: dict[str, Renderer] = {
    "push": render_push,
    "create": render_create,
    "delete": render_delete,
    "pull_request": render_pull_request,
    "pull_request_review": render_pull_request_review,
    "issues": render_issues,
    "issue_comment": render_issue_comment,
    "release": render_release,
    "workflow_run": render_workflow_run,
    "star": render_star,
    "watch": render_star,  # legacy name for the star event
    "fork": render_fork,
    "repository": render_repository,
    "member": render_member,
    "membership": render_member,
    "organization": render_organization,
    "deployment_status": render_deployment_status,
    "discussion": render_discussion,
    "discussion_comment": render_discussion_comment,
    "check_run": render_check_run,
}


_VERBOSE_AWARE = {"pull_request", "pull_request_review", "issues", "issue_comment", "release", "workflow_run", "check_run"}


def render_generic(event: str, p: dict[str, Any], lab: str) -> discord.Embed | None:
    """Verbose fallback: a one-line card for any event we have no dedicated renderer for."""
    action = p.get("action")
    subject = None
    for key in ("pull_request", "issue", "release", "comment", "review", "check_run", "check_suite", "workflow_job",
                "deployment", "discussion", "package", "project", "milestone", "label", "alert", "ref"):
        obj = p.get(key)
        if isinstance(obj, dict):
            subject = obj.get("title") or obj.get("name") or obj.get("tag_name") or obj.get("ref")
            url = obj.get("html_url")
            break
        if isinstance(obj, str):
            subject, url = obj, None
            break
    else:
        url = None
    what = event.replace("_", " ") + (f" {str(action).replace('_', ' ')}" if action else "")
    title = f"[{repo_name(p)}] 📌 {what}" + (f": {truncate(str(subject), 80)}" if subject else "")
    return base_embed(p, title, None, lab, color=GREY, url=url or repo_url(p))


def render(event: str, payload: dict[str, Any], lab_name: str, verbose: bool = False) -> discord.Embed | None:
    fn = RENDERERS.get(event)
    if fn is None:
        return render_generic(event, payload, lab_name) if verbose and repo_name(payload) != "?" else None
    embed = fn(payload, lab_name, verbose) if event in _VERBOSE_AWARE else fn(payload, lab_name)
    if embed is None and verbose and event not in ("push", "delete"):
        embed = render_generic(event, payload, lab_name)
    return embed


def one_liner(event: str, embed: discord.Embed, when: dt.datetime | None = None) -> str:
    when = when or embed.timestamp or dt.datetime.now(dt.timezone.utc)
    title = embed.title or event
    return f"<t:{int(when.timestamp())}:R> [{truncate(title, 70)}]({embed.url})" if embed.url else \
        f"<t:{int(when.timestamp())}:R> {truncate(title, 70)}"


# ----- CI alert state machine -------------------------------------------------

def ci_transition(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """For a completed workflow_run on the default branch return ("fire"|"resolve", fingerprint, info)."""
    if payload.get("action") != "completed":
        return None
    run = payload.get("workflow_run") or {}
    branch = run.get("head_branch")
    if not branch or branch != default_branch(payload):
        return None
    conclusion = run.get("conclusion")
    if conclusion not in ("success", "failure", "timed_out"):
        return None
    repo = repo_name(payload)
    name = run.get("name") or "workflow"
    fp = f"gh:{repo}:workflow:{name}:{branch}"
    info = {"repo": repo, "name": name, "branch": branch, "url": run.get("html_url"),
            "sha": (run.get("head_sha") or "")[:7], "conclusion": conclusion,
            "actor": ((run.get("actor") or {}).get("login") or sender_login(payload))}
    return ("resolve" if conclusion == "success" else "fire", fp, info)


def ci_severity_for(conclusion: str) -> Severity:
    return Severity.OK if conclusion == "success" else Severity.CRITICAL
