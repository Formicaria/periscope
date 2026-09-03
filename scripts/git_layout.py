#!/usr/bin/env python3
"""Per-project git channels: apply permissions + build the repo→channel map.

Convention (by channel name, any category):
  #git-<project>   bot feed for that project's repos: @everyone read-only, @bots may post/embed/manage
  #op-<project>    humans only: @bots may not post here; --purge removes old bot posts

Usage (from /opt/periscope, uses the github bot's token):
  periscope layout --map git-anthill=Anthill,micromound --map git-sovrgnnet=SOVRGNnet.cc \
                   --map git-periscope=periscope --map "git-formicariaus=formicaria*" --purge op-anthill
  add --dry-run to only print what would change.

Prints ready-to-paste .env lines (GITHUB_REPO_CHANNEL_MAP, feed/CI ids) at the end.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import discord
from periscope.layout import apply_git_layout, git_env_lines

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        m = re.match(r"^\s*([A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("  #", 1)[0].strip()
    return out


class Layout(discord.Client):
    def __init__(self, guild_id: int, maps: dict[str, list[str]], purge: list[str], dry: bool):
        super().__init__(intents=discord.Intents.default())
        self.guild_id, self.maps, self.purge_names, self.dry = guild_id, maps, purge, dry
        self.result: dict[str, int] = {}

    async def on_ready(self):
        try:
            guild = self.get_guild(self.guild_id) or await self.fetch_guild(self.guild_id)
            await self.apply(guild)
        finally:
            await self.close()

    async def apply(self, guild: discord.Guild):
        # the logic lives in periscope.layout so the web UI can run it on a connected presence too
        res = await apply_git_layout(guild, me_id=self.user.id if self.user else None, maps=self.maps,
                                     purge=self.purge_names, dry=self.dry, say=print)
        self.result = res.channel_ids
        if res.aborted:
            return
        print("\n# ---- paste into bots/github/.env ----")
        for line in git_env_lines(res, self.maps):
            print(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", action="append", default=[], metavar="git-chan=Repo1,Repo2",
                    help="repos (exact name or glob) that post into a #git-* channel; repeatable")
    ap.add_argument("--purge", action="append", default=[], metavar="op-chan", help="delete bot posts in this channel")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env", default=str(ROOT / "bots" / "github" / ".env"), help="env file with DISCORD_TOKEN + GUILD_ID")
    args = ap.parse_args()

    env = load_env(Path(args.env))
    token, gid = os.environ.get("DISCORD_TOKEN") or env.get("DISCORD_TOKEN"), os.environ.get("GUILD_ID") or env.get("GUILD_ID")
    if not token or not gid:
        # v2: take the github service's presence token + the lab guild from config/periscope.yaml
        try:
            from periscope.store import Store
            store = Store.load(ROOT / "config" / "periscope.yaml")
            token = token or store.token_for("github") or next((p.get("token") for p in store.presences.values() if p.get("token")), "")
            gid = gid or str(store.lab.get("guild_id") or "")
        except Exception:  # noqa: BLE001
            pass
    if not token or not gid:
        print(f"DISCORD_TOKEN / GUILD_ID missing (looked in {args.env} and config/periscope.yaml)", file=sys.stderr)
        return 2
    maps: dict[str, list[str]] = {}
    for item in args.map:
        chan, _, repos = item.partition("=")
        if not repos:
            print(f"bad --map {item!r}: expected git-chan=Repo1,Repo2", file=sys.stderr)
            return 2
        maps.setdefault(chan.strip().lstrip("#"), []).extend(r.strip() for r in repos.split(",") if r.strip())

    Layout(int(gid), maps, args.purge, args.dry_run).run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
