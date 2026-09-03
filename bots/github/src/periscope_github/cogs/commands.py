"""/gh slash commands + live org overview board."""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from periscope import LabBot, PaginatorView, RefreshView, StatusBoard, human_bytes, lab_embed, truncate

from ..client import GithubClient, Reachability
from ..dispatch import get_dispatcher
from ..messages import BOARD_KIND
from ..render import _CONCLUSION, board_ctx, board_embed, parse_ts

log = logging.getLogger(__name__)


# ----- pure formatting helpers (tested) -------------------------------------------

def rel(ts: str | None) -> str:
    d = parse_ts(ts)
    return f"<t:{int(d.timestamp())}:R>" if d else "—"


def pages_from_lines(title: str, lines: list[str], lab_name: str, *, per_page: int = 10,
                     color: int | None = None, url: str | None = None) -> list[discord.Embed]:
    if not lines:
        return [lab_embed(title, "Nothing to show.", lab_name=lab_name, color=color, url=url)]
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    out = []
    for i, chunk in enumerate(chunks, 1):
        e = lab_embed(title if len(chunks) == 1 else f"{title} ({i}/{len(chunks)})", "\n".join(chunk),
                      lab_name=lab_name, color=color, url=url)
        out.append(e)
    return out


def repo_line(r: dict[str, Any]) -> str:
    flags = "".join(x for x in [
        " 🔒" if r.get("private") else "", " 📦" if r.get("archived") else "", " 🍴" if r.get("fork") else ""])
    return (f"**[{r.get('name')}]({r.get('html_url')})**{flags} — ⭐ {r.get('stargazers_count', 0)} • "
            f"🐛 {r.get('open_issues_count', 0)} open • pushed {rel(r.get('pushed_at'))}")


def issue_line(it: dict[str, Any]) -> str:
    repo = (it.get("repository_url") or "").rsplit("/", 1)[-1]
    draft = " (draft)" if it.get("draft") else ""
    user = (it.get("user") or {}).get("login", "?")
    return f"`{repo}` [#{it.get('number')} {truncate(it.get('title', ''), 60)}]({it.get('html_url')}){draft} — {user}, {rel(it.get('updated_at'))}"


def run_line(run: dict[str, Any]) -> str:
    conclusion = run.get("conclusion") or run.get("status") or "?"
    emoji = _CONCLUSION.get(conclusion, ("🔄", 0))[0]
    return (f"{emoji} [{run.get('name')} #{run.get('run_number')}]({run.get('html_url')}) `{run.get('head_branch')}` "
            f"{conclusion} {rel(run.get('updated_at'))}")


def commit_line(c: dict[str, Any]) -> str:
    sha = (c.get("sha") or "")[:7]
    msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0] if c.get("commit") else ""
    author = (c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name") or "?"
    when = ((c.get("commit") or {}).get("author") or {}).get("date")
    return f"[`{sha}`]({c.get('html_url')}) {truncate(msg, 70)} — {author} {rel(when)}"


def language_bar(langs: dict[str, int], top: int = 4) -> str:
    total = sum(langs.values()) or 1
    ranked = sorted(langs.items(), key=lambda kv: -kv[1])[:top]
    return ", ".join(f"{name} {100 * n / total:.0f}%" for name, n in ranked) or "—"


# ----- cog -----------------------------------------------------------------------------

class GithubCommands(commands.Cog):
    gh = app_commands.Group(name="gh", description="GitHub organization feed & queries")

    def __init__(self, bot: LabBot):
        self.bot = bot
        self.cfg = bot.gh_settings  # type: ignore[attr-defined]
        self.client: GithubClient = bot.gh_client  # type: ignore[attr-defined]
        self.dispatcher = get_dispatcher(bot)
        self.reach = Reachability(bot, "GitHub API")
        self.board = StatusBoard(bot, key="github", kind=BOARD_KIND)
        self._repos: list[dict[str, Any]] = []
        self._repos_at = 0.0
        self.status_loop.change_interval(seconds=bot.settings.status_interval_s)
        self.status_loop.start()

    async def cog_unload(self) -> None:
        self.status_loop.cancel()
        await self.client.close()

    # ----- repo cache + autocomplete -----------------------------------------------

    async def repos(self, max_age_s: int = 600) -> list[dict[str, Any]]:
        if time.time() - self._repos_at > max_age_s:
            self._repos = await self.client.list_repos()
            self._repos_at = time.time()
        return self._repos

    async def repo_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            names = [r["name"] for r in await self.repos()]
        except Exception:  # noqa: BLE001
            names = [r["name"] for r in self._repos]
        cur = current.lower()
        return [app_commands.Choice(name=n, value=n) for n in names if cur in n.lower()][:25]

    async def _fail(self, interaction: discord.Interaction, err: Exception) -> None:
        log.warning("/gh command failed: %s", err)
        await self.reach.failure(err)
        msg = f"GitHub API error: `{truncate(str(err), 200)}`"
        if not self.cfg.token:
            msg += "\n(GITHUB_TOKEN is not set — unauthenticated requests are heavily rate-limited.)"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def _paginate(self, interaction: discord.Interaction, pages: list[discord.Embed]) -> None:
        view = PaginatorView(pages, user_id=interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(embed=pages[0], view=view)

    # ----- commands -------------------------------------------------------------------

    @gh.command(name="repos", description="List the organization's repositories")
    async def repos_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            repos = await self.repos(max_age_s=60)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        lines = [repo_line(r) for r in repos]
        pages = pages_from_lines(f"{self.cfg.org}: {len(repos)} repositories", lines, self.bot.lab_name,
                                 url=f"https://github.com/{self.cfg.org}")
        await self._paginate(interaction, pages)

    @gh.command(name="repo", description="Details for one repository")
    @app_commands.describe(name="Repository name")
    @app_commands.autocomplete(name=repo_autocomplete)
    async def repo_cmd(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        try:
            r = await self.client.repo(name)
            langs = await self.client.languages(name)
            prs = await self.client.open_prs(name, per_page=1)
            issues = await self.client.open_issues(name, per_page=1)
            release = await self.client.latest_release(name)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        e = lab_embed(r.get("full_name", name), r.get("description") or "*no description*",
                      lab_name=self.bot.lab_name, url=r.get("html_url"))
        e.add_field(name="Default branch", value=f"`{r.get('default_branch')}`", inline=True)
        e.add_field(name="Open PRs", value=str(prs.get("total_count", 0)), inline=True)
        e.add_field(name="Open issues", value=str(issues.get("total_count", 0)), inline=True)
        e.add_field(name="Stars / Forks", value=f"⭐ {r.get('stargazers_count', 0)} / 🍴 {r.get('forks_count', 0)}",
                    inline=True)
        e.add_field(name="Size", value=human_bytes((r.get("size") or 0) * 1024), inline=True)
        e.add_field(name="Last push", value=rel(r.get("pushed_at")), inline=True)
        e.add_field(name="Languages", value=language_bar(langs), inline=False)
        if release:
            e.add_field(name="Latest release",
                        value=f"[{release.get('name') or release.get('tag_name')}]({release.get('html_url')}) "
                              f"{rel(release.get('published_at'))}", inline=False)
        ci = self.dispatcher.ci_status().get(name)
        if ci:
            e.add_field(name="CI (default branch)", value=f"{'🟢' if ci['ok'] else '🔴'} [{ci['name']}]({ci['url']})",
                        inline=False)
        if r.get("archived"):
            e.title = f"📦 {e.title} (archived)"
        await interaction.followup.send(embed=e)

    @gh.command(name="prs", description="Open pull requests across the org (or one repo)")
    @app_commands.describe(repo="Limit to one repository")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def prs_cmd(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        await interaction.response.defer()
        try:
            data = await self.client.open_prs(repo, per_page=50)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        scope = f"{self.cfg.org}/{repo}" if repo else self.cfg.org
        pages = pages_from_lines(f"Open PRs in {scope}: {data.get('total_count', 0)}",
                                 [issue_line(it) for it in data.get("items", [])], self.bot.lab_name)
        await self._paginate(interaction, pages)

    @gh.command(name="issues", description="Open issues across the org (or one repo)")
    @app_commands.describe(repo="Limit to one repository")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def issues_cmd(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        await interaction.response.defer()
        try:
            data = await self.client.open_issues(repo, per_page=50)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        scope = f"{self.cfg.org}/{repo}" if repo else self.cfg.org
        pages = pages_from_lines(f"Open issues in {scope}: {data.get('total_count', 0)}",
                                 [issue_line(it) for it in data.get("items", [])], self.bot.lab_name)
        await self._paginate(interaction, pages)

    @gh.command(name="runs", description="Latest workflow runs")
    @app_commands.describe(repo="Repository (omit for the 5 most recently pushed repos)")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def runs_cmd(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        await interaction.response.defer()
        lines: list[str] = []
        try:
            targets = [repo] if repo else [r["name"] for r in (await self.repos())[:5]]
            for name in targets:
                runs = await self.client.workflow_runs(name, per_page=10 if repo else 3)
                lines.extend((f"`{name}` " if not repo else "") + run_line(r) for r in runs)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        title = f"Workflow runs: {repo}" if repo else f"Recent workflow runs in {self.cfg.org}"
        await self._paginate(interaction, pages_from_lines(title, lines, self.bot.lab_name))

    @gh.command(name="commits", description="Recent commits on a branch")
    @app_commands.describe(repo="Repository", branch="Branch (default: repo default branch)", n="How many (1-30)")
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def commits_cmd(self, interaction: discord.Interaction, repo: str, branch: str | None = None,
                          n: app_commands.Range[int, 1, 30] = 10) -> None:
        await interaction.response.defer()
        try:
            commits = await self.client.commits(repo, branch, n)
            await self.reach.success()
        except Exception as e:  # noqa: BLE001
            return await self._fail(interaction, e)
        title = f"{repo}@{branch or 'default'}: last {len(commits)} commits"
        url = f"https://github.com/{self.cfg.org}/{repo}/commits/{branch}" if branch else None
        await self._paginate(interaction, pages_from_lines(title, [commit_line(c) for c in commits],
                                                           self.bot.lab_name, url=url))

    @gh.command(name="activity", description="Event counts for the last 24 hours")
    async def activity_cmd(self, interaction: discord.Interaction) -> None:
        summary = self.dispatcher.activity_summary()
        total = sum(summary.values())
        desc = "\n".join(f"`{ev:<22}` {n}" for ev, n in summary.items()) or "No events received in the last 24h."
        e = lab_embed(f"{self.cfg.org} activity (24h): {total} events", desc, lab_name=self.bot.lab_name)
        recent = self.dispatcher.recent()
        if recent:
            e.add_field(name="Latest", value=truncate("\n".join(recent[:5]), 1024), inline=False)
        await interaction.response.send_message(embed=e)

    @gh.command(name="watch", description="(Re)render the live org overview board in STATUS_CHANNEL_ID")
    async def watch_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.bot.settings.status_channel_id:
            return await interaction.followup.send("STATUS_CHANNEL_ID is not configured.", ephemeral=True)
        msg = await self.render_board()
        await interaction.followup.send(f"Board updated: {msg.jump_url}" if msg else "Board render failed (see logs).",
                                        ephemeral=True)

    # ----- status board --------------------------------------------------------------------

    async def board_data(self) -> dict[str, Any]:
        """What the board shows, as plain values: the API's counts (cached ones when it is down) + CI state and
        the latest events from the dispatcher. Also the variables a github.board template can use."""
        try:
            repos = await self.repos()
            prs = await self.client.open_prs(per_page=1)
            issues = await self.client.open_issues(per_page=1)
            await self.reach.success()
            ok = True
        except Exception as e:  # noqa: BLE001
            await self.reach.failure(e)
            repos, prs, issues, ok = self._repos, {}, {}, False
        return board_ctx(self.cfg.org, repos, prs, issues, self.dispatcher.ci_status(), self.dispatcher.recent(),
                         poll=self.cfg.poll_enabled, api_ok=ok)

    async def build_board(self) -> discord.Embed:
        """The board as it should look now, through the user's template — the 🔄 button's builder, so a refresh
        matches the scheduled render (which StatusBoard customises itself). A switched-off board shows plain here;
        the next scheduled render takes it down."""
        data = await self.board_data()
        embed = board_embed(data, self.bot.lab_name)
        return self.bot.messages.apply(BOARD_KIND, embed, data) or embed

    async def render_board(self) -> discord.Message | None:
        try:
            data = await self.board_data()
            return await self.board.render(board_embed(data, self.bot.lab_name), ctx=data,
                                           view=RefreshView(self.build_board, custom_id="periscope:gh:refresh"))
        except Exception:  # noqa: BLE001
            log.exception("status board render failed")
            return None

    @tasks.loop(seconds=60)
    async def status_loop(self) -> None:
        if self.bot.settings.status_channel_id:
            await self.render_board()

    @status_loop.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: LabBot) -> None:
    await bot.add_cog(GithubCommands(bot))
