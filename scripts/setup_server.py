#!/usr/bin/env python3
"""Idempotently create THE LAB's bot-facing categories, channels and roles.

Usage:
  DISCORD_TOKEN=... GUILD_ID=... python scripts/setup_server.py [--dry-run] [--layout layout.json]

Requires a bot in the server with Manage Channels + Manage Roles. Existing items are matched by
name and left alone. Prints a ready-to-paste .env block with channel/role ids at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import discord

DEFAULT_LAYOUT = {
    "roles": [
        {"name": "lab-admin", "color": 0xE67E22, "mentionable": False},
        {"name": "lab-oncall", "color": 0xE74C3C, "mentionable": True},
        {"name": "formicaria-dev", "color": 0x2ECC71, "mentionable": True},
        {"name": "bots", "color": 0x5865F2, "mentionable": False},
    ],
    "categories": [
        {
            "name": "🧪 LAB STATUS",
            "channels": [
                {"name": "lab-status", "topic": "Live boards from every lab. Bots edit in place; humans read.", "env": "STATUS_CHANNEL_ID"},
                {"name": "lab-alerts", "topic": "Firing / resolved alerts. Criticals ping @lab-oncall.", "env": "ALERT_CHANNEL_ID"},
                {"name": "media", "topic": "Grabs, downloads, now playing.", "env": "MEDIA_CHANNEL_ID"},
                {"name": "network", "topic": "UniFi: new clients, device status, firmware.", "env": "NETWORK_CHANNEL_ID"},
                {"name": "backups", "topic": "Proxmox backup summaries.", "env": "BACKUP_CHANNEL_ID"},
            ],
        },
        {
            "name": "🕹️ LAB CONTROL",
            "channels": [
                {"name": "lab-cmd", "topic": "Run /pve /prom /arr /unifi /gh here.", "env": "CMD_CHANNEL_ID"},
            ],
        },
        {
            "name": "Formicaria",
            "channels": [
                {"name": "formicaria-git", "topic": "Every push, PR, issue, release and CI run in the Formicaria org.", "env": "GITHUB_FEED_CHANNEL_ID"},
                {"name": "formicaria-ci", "topic": "CI failures on default branches. Pings @formicaria-dev.", "env": "GITHUB_CI_CHANNEL_ID"},
            ],
        },
    ],
}


def norm(s: str) -> str:
    return s.strip().lower()


class Setup(discord.Client):
    def __init__(self, guild_id: int, layout: dict, dry_run: bool):
        super().__init__(intents=discord.Intents.default())
        self.guild_id = guild_id
        self.layout = layout
        self.dry_run = dry_run
        self.env_out: dict[str, int] = {}

    async def on_ready(self):
        try:
            guild = self.get_guild(self.guild_id) or await self.fetch_guild(self.guild_id)
            await self.apply(guild)
        finally:
            await self.close()

    async def apply(self, guild: discord.Guild):
        tag = "[dry-run] " if self.dry_run else ""
        roles = {norm(r.name): r for r in await guild.fetch_roles()}
        for spec in self.layout["roles"]:
            r = roles.get(norm(spec["name"]))
            if r is None:
                print(f"{tag}create role @{spec['name']}")
                if not self.dry_run:
                    r = await guild.create_role(name=spec["name"], color=discord.Color(spec["color"]),
                                                mentionable=spec.get("mentionable", False), reason="periscope setup")
            else:
                print(f"exists role @{r.name} ({r.id})")
            if r:
                self.env_out[spec["name"].upper().replace("-", "_") + "_ROLE_ID"] = r.id

        channels = await guild.fetch_channels()
        cats = {norm(c.name): c for c in channels if isinstance(c, discord.CategoryChannel)}
        texts = {norm(c.name): c for c in channels if isinstance(c, discord.TextChannel)}

        bots_role = roles.get("bots")
        for cspec in self.layout["categories"]:
            cat = cats.get(norm(cspec["name"]))
            if cat is None:
                print(f"{tag}create category {cspec['name']}")
                if not self.dry_run:
                    cat = await guild.create_category(cspec["name"], reason="periscope setup")
            else:
                print(f"exists category {cat.name} ({cat.id})")
            for ch in cspec["channels"]:
                existing = texts.get(norm(ch["name"]))
                if existing is None:
                    print(f"{tag}  create #{ch['name']}")
                    if not self.dry_run:
                        overwrites = {}
                        if bots_role and cspec["name"].endswith("LAB STATUS"):
                            overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
                            overwrites[bots_role] = discord.PermissionOverwrite(
                                send_messages=True, embed_links=True, attach_files=True, manage_messages=True)
                        existing = await guild.create_text_channel(
                            ch["name"], category=cat, topic=ch.get("topic"), overwrites=overwrites,
                            reason="periscope setup")
                else:
                    print(f"  exists #{existing.name} ({existing.id})")
                if existing:
                    self.env_out[ch["env"]] = existing.id

        print("\n# ---- paste into .env ----")
        print(f"GUILD_ID={guild.id}")
        for k, v in self.env_out.items():
            print(f"{k}={v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--layout", type=Path, help="JSON file overriding the default layout")
    args = ap.parse_args()
    token = os.environ.get("DISCORD_TOKEN")
    gid = os.environ.get("GUILD_ID")
    if not token or not gid:
        print("DISCORD_TOKEN and GUILD_ID env vars are required", file=sys.stderr)
        return 2
    layout = json.loads(args.layout.read_text()) if args.layout else DEFAULT_LAYOUT
    Setup(int(gid), layout, args.dry_run).run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
