"""The github bot's message kinds: every card it posts, registered for the Messages page with a sample to preview
and customise it from.

Registering here is what lists a kind on the page. The send sites — `dispatch.py` for the feed cards, `train.py`
for the live CI card, the board cog for the org overview — pass each embed through `bot.messages.apply(kind, embed,
ctx)` right before posting, with the same ctx a kind's sample returns here. CI-failure alerts go through
`bot.alerts` and are customised as the core `core.alert` kind, so they are not listed again.
"""

from __future__ import annotations

from typing import Any

import discord
from periscope.messages import MessageKind, register

from . import samples
from .render import board_ctx, board_embed, event_ctx, render_event
from .train import KIND as TRAIN_KIND
from .train import render_train, train_ctx

SERVICE = "github"
BOARD_KIND = "github.board"
LAB = "my-lab"   # the lab name previews carry; a real post carries the bot's

FEED_WHERE = "the repo's own channel (GITHUB_REPO_CHANNEL_MAP), else the feed channel"
CI_WHERE = "the repo's own channel (GITHUB_REPO_CHANNEL_MAP), else the CI channel"
CI_ENV = "GITHUB_CI_CHANNEL_ID"


def feed_kind(name: str) -> str:
    """The kind a feed card is customised under: `github.<event>`, or `github.other` for the generic card."""
    return f"{SERVICE}.{name}"


# ----- samples --------------------------------------------------------------------------------------------
def _feed_sample(name: str):
    def sample() -> tuple[discord.Embed | None, dict[str, Any]]:
        event, payload = samples.payload(name)
        kind, embed = render_event(event, payload, LAB, verbose=True)
        return embed, event_ctx(kind, event, payload)
    return sample


def _sample_train() -> tuple[discord.Embed | None, dict[str, Any]]:
    repo, run, jobs = samples.train()
    return (render_train(repo, run, jobs, LAB, now=samples.TRAIN_NOW),
            train_ctx(repo, run, jobs, now=samples.TRAIN_NOW))


def _sample_board() -> tuple[discord.Embed | None, dict[str, Any]]:
    data = board_ctx(**samples.board())
    return board_embed(data, LAB), data


# ----- variables (name → meaning, shown next to the editor) ------------------------------------------------
COMMON = {
    "event": "the GitHub event name (push, pull_request, …)",
    "action": "what happened (opened, closed, …), when the event has one",
    "repo": "the repository's name", "repo_full": "owner/name", "repo_url": "the repository on GitHub",
    "org": "the organization", "sender": "who did it (GitHub login)", "sender_url": "their profile",
    "sender_avatar": "their avatar image url",
}
BODY = "the text they wrote (first 1000 characters)"
NUMBER = "the number (#42)"


def _feed(name: str, title: str, description: str, variables: dict[str, str], *, where: str = FEED_WHERE,
          where_env: str = "GITHUB_FEED_CHANNEL_ID", group: str = "feed") -> MessageKind:
    return MessageKind(feed_kind(name), title, description, where=where, where_env=where_env, group=group,
                       sample=_feed_sample(name), variables={**COMMON, **variables})


register(
    MessageKind(BOARD_KIND, "Org overview board",
                "the pinned board: repository count, open PRs and issues, CI health per repository and the latest "
                "events; refreshed every STATUS_INTERVAL_S and by its 🔄 button",
                where="the status channel", where_env="STATUS_CHANNEL_ID", sample=_sample_board, group="boards",
                variables={"org": "the organization", "org_url": "the organization on GitHub",
                           "repo_count": "how many repositories",
                           "open_prs": "open pull requests across the org (empty when the API is down)",
                           "open_issues": "open issues across the org (empty when the API is down)",
                           "ci": "CI on each default branch, failing first: item.repo · item.ok · item.workflow · item.url",
                           "failing": "the repositories whose CI is red",
                           "recent": "the latest events as one-liners, newest first",
                           "source": "webhook, or webhook + poll", "api_ok": "false when GitHub could not be reached"}),
    _feed("push", "Push", "posted for every push to a mapped repository (not for branch deletions or empty pushes)",
          {"branch": "the branch pushed to", "pusher": "who pushed", "commit_count": "how many commits",
           "commits": "the commits: item.sha · item.message · item.author · item.url",
           "head_sha": "the new tip, short", "compare_url": "the compare view on GitHub",
           "forced": "true for a force-push"}),
    _feed("create", "Branch or tag created", "a branch or tag was created",
          {"ref_type": "branch or tag", "ref": "its name", "ref_url": "the branch or tag on GitHub"}),
    _feed("delete", "Branch or tag deleted", "a branch or tag was deleted",
          {"ref_type": "branch or tag", "ref": "its name", "ref_url": "where it was on GitHub"}),
    _feed("pull_request", "Pull request",
          "a pull request was opened, closed or merged, reopened, marked ready or draft, or had a review requested "
          "(every action in verbose mode)",
          {"number": NUMBER, "pr_title": "the pull request's title", "pr_author": "who opened it",
           "pr_url": "the pull request on GitHub", "base": "the branch it merges into",
           "head": "the branch it comes from", "head_sha": "its latest commit, short", "draft": "true for a draft",
           "merged": "true once merged", "additions": "lines added", "deletions": "lines removed",
           "changed_files": "files touched", "labels": "its labels as a list",
           "reviewer": "who was asked to review, if that is the action", "body": BODY}),
    _feed("pull_request_review", "Pull request review",
          "someone approved, requested changes on or commented on a pull request",
          {"number": NUMBER, "pr_title": "the pull request's title", "pr_url": "the pull request on GitHub",
           "state": "approved · changes_requested · commented", "reviewer": "who reviewed",
           "review_url": "the review on GitHub", "body": BODY}),
    _feed("issues", "Issue",
          "an issue was opened, closed, reopened, labeled, assigned or pinned (every action in verbose mode)",
          {"number": NUMBER, "issue_title": "the issue's title", "issue_author": "who opened it",
           "issue_url": "the issue on GitHub", "state": "open or closed", "labels": "its labels as a list",
           "assignees": "who it is assigned to, as a list", "label": "the label added or removed, if that is the action",
           "assignee": "who was assigned or unassigned, if that is the action", "body": BODY}),
    _feed("issue_comment", "Issue or PR comment", "a new comment on an issue or pull request",
          {"number": NUMBER, "issue_title": "the issue's (or pull request's) title", "issue_url": "the issue on GitHub",
           "is_pr": "true when the comment is on a pull request", "commenter": "who commented",
           "comment_url": "the comment on GitHub", "body": BODY}),
    _feed("release", "Release", "a release was published (every release action in verbose mode)",
          {"release_name": "the release's name (its tag when unnamed)", "tag": "the tag",
           "release_url": "the release on GitHub", "prerelease": "true for a pre-release", "draft": "true for a draft",
           "release_author": "who published it", "asset_count": "how many files are attached",
           "assets": "the attached files: item.name · item.size · item.url", "body": BODY}),
    _feed("workflow_run", "Workflow run",
          "an Actions workflow run finished (also started, in verbose mode); runs shown as a live CI train card are "
          "not posted again",
          {"workflow": "the workflow's name", "branch": "the branch it ran on",
           "on_default_branch": "true when that is the default branch", "run_number": "the run number",
           "status": "queued · in_progress · completed", "conclusion": "success · failure · cancelled · …",
           "run_url": "the run on GitHub", "sha": "the commit it ran for, short", "commit_title": "that commit's title",
           "trigger": "what started it (push, pull_request, schedule, …)", "actor": "who started it",
           "duration": "how long it took, in words", "duration_s": "how long it took, in seconds"},
          where=CI_WHERE, where_env=CI_ENV, group="ci"),
    _feed("star", "Star", "someone starred a repository", {"stars": "the repository's star count now"}),
    _feed("fork", "Fork", "someone forked a repository",
          {"fork_full": "the fork as owner/name", "fork_url": "the fork on GitHub", "fork_owner": "who forked it",
           "forks": "the repository's fork count"}),
    _feed("repository", "Repository",
          "a repository was created, deleted, archived or unarchived, renamed, made public or private, or transferred",
          {"repo_description": "the repository's description", "private": "true for a private repository",
           "old_name": "the previous name, when renamed"}),
    _feed("member", "Collaborator", "someone was added to or removed from a repository or team",
          {"member": "who was added or removed", "member_url": "their profile",
           "team": "the team, when it is a team change", "scope": "what they were added to or removed from, in words"}),
    _feed("organization", "Organization member", "someone joined, left or was invited to the organization",
          {"member": "who joined, left or was invited", "member_url": "their profile",
           "verb": "joined · left · was invited to"}),
    _feed("deployment_status", "Deployment", "a deployment finished: success, failure, error or inactive",
          {"environment": "the environment deployed to", "state": "success · failure · error · inactive",
           "ref": "what was deployed", "sha": "the deployed commit, short",
           "state_description": "GitHub's note on the state", "environment_url": "the live environment",
           "deploy_url": "the deployment's logs or target"}),
    _feed("discussion", "Discussion", "a discussion was created, answered or closed",
          {"number": NUMBER, "discussion_title": "the discussion's title", "discussion_url": "the discussion on GitHub",
           "category": "its category", "discussion_author": "who started it", "body": BODY}),
    _feed("discussion_comment", "Discussion comment", "a new comment on a discussion",
          {"number": NUMBER, "discussion_title": "the discussion's title", "discussion_url": "the discussion on GitHub",
           "commenter": "who commented", "comment_url": "the comment on GitHub", "body": BODY}),
    _feed("check_run", "Check failed", "a check run failed or timed out (every completed check in verbose mode)",
          {"check": "the check's name", "status": "the check's status", "conclusion": "failure · timed_out · …",
           "sha": "the commit it checked, short", "check_url": "the check on GitHub",
           "output_title": "the check's own headline", "output_summary": "the check's own summary (first 1000 characters)"},
          where=CI_WHERE, where_env=CI_ENV, group="ci"),
    _feed("other", "Any other event",
          "verbose mode's one-line card for events without a card of their own (milestones, labels, packages, wiki "
          "pages, …)",
          {"subject": "what the event is about (a title, name or tag), when GitHub names one",
           "subject_url": "where that is on GitHub"}),
    MessageKind(TRAIN_KIND, "CI train",
                "the live card for a running Actions workflow: posted when the run starts and edited in place as its "
                "jobs progress, until the final result",
                where=CI_WHERE, where_env=CI_ENV, sample=_sample_train, group="ci",
                variables={"repo": "the repository's name", "workflow": "the workflow's name",
                           "branch": "the branch it runs on", "run_number": "the run number",
                           "status": "queued · in_progress · completed",
                           "conclusion": "success · failure · cancelled · … once finished",
                           "done": "true once the run has finished", "run_url": "the run on GitHub",
                           "sha": "the commit it runs for, short", "commit_title": "that commit's title",
                           "actor": "who started it", "actor_url": "their profile", "actor_avatar": "their avatar image url",
                           "elapsed": "how long it has been running (or took), in words",
                           "jobs": "the jobs: item.name · item.status · item.conclusion · item.state · item.line (as drawn)",
                           "jobs_done": "how many jobs have finished", "job_count": "how many jobs there are"}),
)
