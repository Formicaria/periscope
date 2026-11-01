# periscope · community

The public face of the lab in a members' Discord server. The other services talk to the people who run the
boxes; this one talks to the people who *use* them — so "is Plex down?" is answered before anyone asks.

| Does | How |
|---|---|
| **Status board** | one pinned board in the PSA channel: every resource up / down / in maintenance, with the reason, plus what's scheduled |
| **Outage posts that say why** | something down for `OUTAGE_GRACE_S` gets one post — "pve3 is offline", "Plex's container is stopped", "media storage is down", "the server is up but Plex isn't answering" — worked out from Proxmox. One post per root cause listing everything it took down; edited as the picture changes; flipped to **Resolved** with a "back up" reply when it's over |
| **Maintenance** | `/psa maint schedule fri 10pm 2h "Moving Plex to pve2" Plex` posts the notice with a countdown in everyone's own time zone, reminds an hour ahead, says when it starts and when it's done, and keeps periscope's own alerts quiet for the window |
| **Announcements** | `/psa post` (a form for the long text), or drop a `.md` file in `PSA_OUTBOX` and it goes out on the next pass. Announcements and changelogs always ping `ANNOUNCE_PING` (@everyone) |
| **Pages** | One `.md` per channel in `PAGES_DIR` (front matter `channel: <id>`) becomes clean embeds there, one card per `# ` heading. Edit the file and the post is edited in place: no repost, no ping. For #welcome, #rules, #faq, #guides |
| **Who gets pinged** | outages and maintenance ping the roles of what they hit (the last field of each RESOURCES entry); announcements ping everyone |
| **Linking** | `/link minecraft` and `/link plex` tie a member's Discord to their game / Plex account and give them that thing's role — see below |

## Commands (admin role only)

```
/psa post
/psa maint schedule  when duration reason [affects]   when: now · in 2h · 10pm · tomorrow 9am · fri 22:00 · 2026-10-30 22:00
/psa maint now       duration reason [affects]        duration: 30m · 2h · 1h30m
/psa maint extend    id duration
/psa maint done      id
/psa maint cancel    id
/psa maint list
```

`affects` autocompletes from the resource names; leave it empty for "everything". Times are read in `TIMEZONE`.

## RESOURCES

One string, entries separated by `;` — `Name | check | target | guest | depends on | roles to ping`:

```
Plex | plex | http://10.10.10.39:32400 | ct:139 | Media storage | plex members;
Media storage | tcp | 10.10.10.140:2049 | vm:137 | | plex members;
Requests | http | http://10.10.10.62:5055 | ct:119 | | plex members;
Minecraft: Horror | minecraft | 10.200.0.229:25565 | ct:127 | | horror players;
Twingate | guest | | ct:105
```

| check | up when |
|---|---|
| `plex` | `GET <url>/identity` answers 200 (no token needed); the board shows the version |
| `http` | any answer below 500 — a login page or a 401 still means the app is alive |
| `tcp` | the port accepts a connection |
| `minecraft` | the port accepts a connection; the Server List Ping adds players and version to the board |
| `guest` | no network probe — the Proxmox guest is running and its node is online |

`guest` (`ct:139` / `vm:137`) is what lets the post say *why*. `depends on` folds an outage into its cause: when
the storage VM stops, the post is "Media storage is stopped — affects Media storage and Plex", not two posts.

A resource that is already down the very first time periscope looks at it stays board-only until it has been up
once — nobody needs a PSA about a server that was switched off on purpose.

## Linking (`/link`)

Anyone can run these; the role they get is also what makes outage and maintenance pings reach them.

```
/link minecraft     a one-time code — type `link CODE` in game chat on any of our servers
/link plex          sign in on plex.tv; an account that has this server gets LINK_PLEX_ROLE
/link status        what you're linked to
/link remove        forget your Minecraft names (their roles go too) or your Plex link (the role stays)
/link list | sync   admins: who is linked to what · give everyone their roles back
```

**Minecraft** needs nothing on the game side. Each server already relays its chat into a Discord channel through
its own bridge bot, so a code typed in game shows up in Discord *as said by that player* — that's the proof.
`LINK_MINECRAFT` lists the bridges, `Resource name | chat channel id [| roles]` (roles default to the resource's
ping roles). Once a player is linked, the bridge saying they joined or chatted on another of our servers gives
them that server's role too. Bridge bots' own roles (the ones Discord creates for each bot) can't be given to
people — make a normal role per server.

**Plex** uses plex.tv's PIN sign-in (the one every Plex companion app uses). periscope keeps the account's id
and name, checks it against the server's share list with the token it borrows from `PLEX_FROM_SERVICE`, and
throws the member's sign-in token away. The Plex role is only ever *added*: taking it from someone invited by
`/plexinvite` would pull their share (plexrequests' auto-revoke), so unlinking leaves it alone.

The service needs the Server Members and Message Content intents (it declares them) and Manage Roles, with its
own role above the ones it hands out.

## Proxmox access

Borrowed from another periscope service (`PVE_FROM_SERVICE`, default `proxmox`) when it is needed, straight out
of `config/periscope.yaml` — one token to rotate, nothing to copy. Blank it and outages say *what*, not *why*.

## Where things go

* Posts, the board and announcements: `PSA_CHANNEL_ID`. Make it read-only for members; the bot needs Send
  Messages, Embed Links, Read Message History and Manage Messages (to pin the board).
* Each outage is also fired as a normal periscope alert in the server's alert channel, for the admins.
* Maintenance windows land in `config/maintenance.yaml` as `psa-<id>` and show up on the Alerts page.
