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
