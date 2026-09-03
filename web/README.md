# periscope web UI

Admin pages + JSON API served **inside the periscope runtime** (`python -m periscope`) on `web.port`
(default `:8090`, see `config/periscope.yaml`). Server-rendered Jinja templates, HTMX partial swaps,
Tailwind + daisyUI from a CDN — no Node, no build step.

```
./venv/bin/pip install -e web        # setup.sh / update.sh do this
periscope web                        # prints the URL
```

## Sign-in

Discord OAuth2. Allowed = members of the default server holding any of `web.allowed_role_ids`
(defaulting to that server's `admin_role_ids`; when both are empty, the guild owner). The Discord application needs
`<base_url>/auth/callback` as a redirect URL (developer portal → OAuth2).

First run: `periscope web` on the box prints a one-time link (`/login?token=…`; the token also lands in
`data/web-setup-token`, mode 0600, until used) that signs you in as bootstrap admin. The login page accepts the
same token by hand and can store the OAuth client id/secret + base URL for Discord sign-in. `PERISCOPE_WEB_NOAUTH=1` disables the sign-in
entirely for local development (loud warning in the log).

## Pages

| path | what |
|---|---|
| `/` | "needs attention" list with a fix link per problem; every service as a card (plain-language state, the bot it posts as and the server it posts in) with Switch on/off / Test |
| `/services/<name>` | the service's typed settings as a form, plus the bot it posts as and the server it posts in; Test runs `check()` on the submitted values |
| `/presences` | **Bots**: identities (tokens, checked against Discord), invite links, which services post as which, why one is offline |
| `/discord` | **Servers**: one card per Discord server (name, colour, ids, channels, roles), add/remove/mark default, the shared settings, web sign-in settings, channel layout (create missing, apply git/op permissions) |
| `/messages` | every post a bot makes, previewed as Discord draws it: reword it (Simple or raw-JSON tab), switch it off, send a test post |
| `/routing` | GitHub repo → channel map, feed / CI catch-alls, per-service alert routing |
| `/logs` | live log tail (SSE), filter by service, download |
| `/setup` | first-run flow: token → invite → pick server → channel layout → add services |
| `/api/status`, `/api/config`, `/healthz` | JSON (secrets masked) |

Config edits are written to `config/periscope.yaml` and apply on the next restart (the header shows
a "restart to apply" banner; Restart re-executes the process after one second). Message customisations are
the exception: they go to `config/messages.yaml`, which the bots re-read before every post, so `/messages`
never raises that banner.
