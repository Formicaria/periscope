# Discord applications

One application per bot. Invite each to THE LAB with the link below (scopes `bot` + `applications.commands`; permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Mention Everyone = `224256`).

| Bot | Application ID | Invite |
|---|---|---|
| Proxmox | `1544461168410763354` | https://discord.com/oauth2/authorize?client_id=1544461168410763354&scope=bot%20applications.commands&permissions=224256 |
| Prometheus | `1544461789553754234` | https://discord.com/oauth2/authorize?client_id=1544461789553754234&scope=bot%20applications.commands&permissions=224256 |
| Arr | `1544461919279128586` | https://discord.com/oauth2/authorize?client_id=1544461919279128586&scope=bot%20applications.commands&permissions=224256 |
| Unifi | `1544462016427724830` | https://discord.com/oauth2/authorize?client_id=1544462016427724830&scope=bot%20applications.commands&permissions=224256 |
| Github | `1544462125844533358` | https://discord.com/oauth2/authorize?client_id=1544462125844533358&scope=bot%20applications.commands&permissions=224256 |

Tokens: Developer Portal → app → Bot → **Reset Token**. Paste into `bots/<bot>/.env` as `DISCORD_TOKEN`. No privileged intents are needed.

Other members self-hosting the same bot reuse these applications: the same token can run on several hosts at once (Discord allows multiple gateway sessions per bot), and `LAB_NAME` distinguishes them. If a member wants their own bot identity instead, they create their own application and invite it.

## THE LAB — channel and role IDs (created 2026-09-01)

| Env var | Channel / role | ID |
|---|---|---|
| `GUILD_ID` | THE LAB | `1439743165845344468` |
| `STATUS_CHANNEL_ID` | `#lab-status` | `1544507153837195354` |
| `ALERT_CHANNEL_ID` | `#lab-alerts` | `1544507301199749161` |
| `MEDIA_CHANNEL_ID` (arr) | `#media` | `1544507357021741177` |
| `ALERT_CHANNEL_ID` (unifi, optional) | `#network` | `1544507409186295888` |
| — | `#backups` | `1544507456791781376` |
| — | `#lab-cmd` (slash commands) | `1544507631278751744` |
| `GITHUB_FEED_CHANNEL_ID` | `#formicaria-git` | `1544507700895809627` |
| `ALERT_CHANNEL_ID` (github) | `#formicaria-ci` | `1544507752900984882` |
| `ADMIN_ROLE_IDS` | `@lab-admin` | `1544507932664922152` |
| `ALERT_ROLE_ID` | `@lab-oncall` | `1544508010909671454` |
| `GITHUB_CI_FAILURE_ROLE_ID` | `@formicaria-dev` | `1544508111442681866` |
| — | `@bots` | `1544508257123434526` |

Per-repo routing for the github bot: `GITHUB_REPO_CHANNEL_MAP=Anthill=1508373809118314636,micromound=1538050048779092040,SOVRGNnet.cc=1481112431894855680`
