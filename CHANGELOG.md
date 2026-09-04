# Changelog

Every release is a tag (`vX.Y.Z`) whose section here becomes the GitHub release notes. Every package in the
repo carries the same version — the eight `__init__.py` files and the eight `pyproject.toml` files — and the
release workflow refuses a tag that disagrees with any of them or has no section here.

## v0.1.2

**THE INSTALL WOULD NOT UPDATE.** Renumbering to 0.1.1 left every package still requiring `periscope>=1.0`, so
`periscope update` stopped at "conflicting dependencies" and nothing was installed. Core, the web UI and all six
service packages now carry the same version and require `periscope>=0.1`, and the release workflow refuses a tag
unless every `__init__.py` and every `pyproject.toml` in the repo agrees with it — this cannot ship again.

## v0.1.1

**BOTS, LAID OUT PER DISCORD SERVER.** The Bots page was one flat table, so an install posting in two Discord
servers gave no clue which bot lived where. It is now one section per server — heading, Discord id, the bots
that post there, each row editable in place as before. A bot that serves several servers appears in each
section and says so; a bot nothing uses yet waits under "Not in use". Each row also states whether that bot is
actually a member of that server, with its invite link when it is not.

**A SERVICE'S OWN DISCORD SERVER BECOMES A SERVER.** Configs written before multiple servers existed hid a
second Discord server inside a service's settings (the Plex request service carried its own `GUILD_ID`). On
load, any such service is promoted to a server of its own — its channels come along, the override is dropped,
and the upgraded file is written back so the config and the UI agree. Two servers, both visible, both editable.

**SERVERS ARE TOLD APART BY THEIR REAL DISCORD NAME.** A server's `name` is only the wording embed footers
carry; two servers can share one. Cards now lead with the real Discord name (fetched from the guild) above the
id and key, the display-name field says what it actually is, and one click copies the Discord name into it.
Everywhere else a server is named — the "in server" picker, the Overview cards, routing rows, toasts — reads
`display name (Real Discord Name)` when the two differ.

**EVERY POST IS EDITABLE, WITH A PREVIEW.** A new Messages page lists every kind of post the bots make — status
boards, alerts, the whole GitHub feed, media grabs and imports, the Plex invite and request embeds — drawn the
way Discord draws them. *Simple* is a form (title, text, colour, footer, extra fields) with clickable
`{{ variables }}`; *Code* is the raw template, embed-shaped JSON with sandboxed Jinja (`if`, `repeat`, filters).
Live preview beside the shipped default, reset, switch a kind off, and a test post into its real channel.
Customisations live in `config/messages.yaml` and apply to the next post — no restart. A broken template never
blocks a post: the bot's own version goes out and the error is logged.

**MANY DISCORD SERVERS FROM ONE INSTALL.** `servers:` in `config/periscope.yaml` holds one entry per Discord
server (name, colour, id, status/alert channels, admin roles). Every service names the server it posts in, and
a bot serves every server its services use, registering its slash commands in each. Settings that are not
per-server — log level, board refresh — moved to their own card. The UI says "server" throughout; only the
`#lab-status` / `@lab-admin` names the channel convention creates keep the old word.

**HOUSEKEEPING.** `periscope list` gained a SERVER column and the bots' servers; `periscope init` asks for a
server name; the CI workflow's core job no longer needs the bot packages installed, and the actions run on
Node 24 with Python 3.13.

## v0.1.0

The first numbered release of the rebuilt periscope: one process and one systemd unit for every integration,
a YAML config store, bots (Discord identities) shared by services, the web UI on :8090 with a one-time
sign-in link, plain-language service states with a "needs attention" list, status boards that stay a single
message, and the standalone Plex bot folded in as the `plexrequests` service.
