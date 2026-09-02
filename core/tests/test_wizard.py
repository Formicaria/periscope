"""End-to-end run of the init wizard against a fake Discord + fake PVE, driven by scripted answers."""

import json
import sys

import pytest

from periscope import wizard

EXAMPLE = """# ---------- Discord ----------
DISCORD_TOKEN=
LAB_NAME=my-lab
LAB_COLOR=5865F2                 # Hex color used for INFO embeds
GUILD_ID=
ALERT_CHANNEL_ID=
STATUS_CHANNEL_ID=
ALERT_ROLE_ID=
ADMIN_ROLE_IDS=
DATA_DIR=data
WEBHOOK_PORT=8080
PVE_URL=https://pve.example.lan:8006
PVE_TOKEN_ID=periscope@pve!discord
PVE_TOKEN_SECRET=
PVE_VERIFY_SSL=false
"""


class FakeWorld:
    """Minimal Discord + PVE REST behaviour."""

    def __init__(self):
        self.channels = []
        self.roles = [{"id": "1", "name": "@everyone"}]
        self.created = []
        self.next_id = 1000

    def _id(self):
        self.next_id += 1
        return str(self.next_id)

    def http(self, method, url, headers=None, body=None, verify=True, timeout=15):
        auth = (headers or {}).get("Authorization", "")
        if url.startswith(wizard.DISCORD_API):
            path = url[len(wizard.DISCORD_API):]
            if auth != "Bot good-token":
                return 401, {"message": "401: Unauthorized"}
            if path == "/users/@me":
                return 200, {"id": "999", "username": "Proxmox"}
            if path == "/users/@me/guilds":
                return 200, [{"id": "42", "name": "THE LAB"}]
            if path == "/guilds/42/channels" and method == "GET":
                return 200, self.channels
            if path == "/guilds/42/roles" and method == "GET":
                return 200, self.roles
            if path == "/guilds/42/channels" and method == "POST":
                c = {"id": self._id(), "name": body["name"], "type": body["type"]}
                self.channels.append(c)
                self.created.append(("channel", body["name"]))
                return 201, c
            if path == "/guilds/42/roles" and method == "POST":
                r = {"id": self._id(), "name": body["name"]}
                self.roles.append(r)
                self.created.append(("role", body["name"]))
                return 201, r
        if url.endswith("/api2/json/version"):
            if auth == "PVEAPIToken=periscope@pve!discord=s3cret" and not verify:
                return 200, {"data": {"version": "8.2"}}
            return 401, "authentication failure"
        return 0, "unreachable"


def run_wizard(tmp_path, monkeypatch, answers, secrets_, world):
    root = tmp_path
    (root / "bots" / "proxmox").mkdir(parents=True)
    (root / "bots" / "proxmox" / ".env.example").write_text(EXAMPLE)
    answers = iter(answers)
    secrets_ = iter(secrets_)
    monkeypatch.setattr(wizard, "http", world.http)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": next(secrets_))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    calls = []
    monkeypatch.setattr(wizard.subprocess, "run", lambda *a, **k: calls.append(a[0]) or type("R", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr(sys, "argv", ["wizard", str(root), "proxmox"])
    rc = wizard.main()
    return rc, root, calls


def test_full_run_creates_layout_and_env(tmp_path, monkeypatch):
    world = FakeWorld()
    answers = [
        "y",                      # create missing channels/roles
        "homelab",                # lab name
        "",                       # color default
        "https://10.0.0.5:8006",  # PVE URL
        "",                       # token id default
        "y",                      # enable now
    ]
    secrets_ = ["bad-token", "good-token", "s3cret"]
    rc, root, calls = run_wizard(tmp_path, monkeypatch, answers, secrets_, world)
    assert rc == 0
    env = wizard.load_env(root / "bots" / "proxmox" / ".env")
    assert env["DISCORD_TOKEN"] == "good-token"
    assert env["GUILD_ID"] == "42"
    assert env["LAB_NAME"] == "homelab"
    assert env["PVE_URL"] == "https://10.0.0.5:8006"
    assert env["PVE_TOKEN_SECRET"] == "s3cret"
    names = {n for _, n in world.created}
    assert {"lab-status", "lab-alerts", "lab-admin", "lab-oncall", "🧪 LAB STATUS"} <= names
    by_name = {c["name"]: c["id"] for c in world.channels}
    assert env["STATUS_CHANNEL_ID"] == by_name["lab-status"]
    assert env["ALERT_CHANNEL_ID"] == by_name["lab-alerts"]
    roles = {r["name"]: r["id"] for r in world.roles}
    assert env["ALERT_ROLE_ID"] == roles["lab-oncall"] and env["ADMIN_ROLE_IDS"] == roles["lab-admin"]
    shared = json.loads((root / "periscope.json").read_text())
    assert shared["GUILD_ID"] == "42" and shared["STATUS_CHANNEL_ID"] == by_name["lab-status"]
    assert calls and calls[0][:2] == ["systemctl", "enable"]
    # full-line comments from the example survive, inline ones never reach the value line
    text = (root / "bots" / "proxmox" / ".env").read_text()
    assert "# ---------- Discord ----------" in text
    assert not any("  #" in line for line in text.splitlines() if "=" in line and not line.startswith("#"))


def test_second_run_reuses_shared_and_existing_layout(tmp_path, monkeypatch):
    world = FakeWorld()
    world.channels = [{"id": "c1", "name": "lab-status", "type": 0}, {"id": "c2", "name": "lab-alerts", "type": 0}]
    world.roles += [{"id": "r1", "name": "lab-oncall"}, {"id": "r2", "name": "lab-admin"}]
    (tmp_path / "periscope.json").write_text(json.dumps({"GUILD_ID": "42", "LAB_NAME": "homelab", "LAB_COLOR": "00D4FF"}))
    answers = ["", "", "https://10.0.0.5:8006", "", "n"]  # lab name + color defaults, pve, no enable
    rc, root, calls = run_wizard(tmp_path, monkeypatch, answers, ["good-token", "s3cret"], world)
    assert rc == 0
    env = wizard.load_env(root / "bots" / "proxmox" / ".env")
    assert env["STATUS_CHANNEL_ID"] == "c1" and env["ALERT_ROLE_ID"] == "r1" and env["LAB_NAME"] == "homelab"
    assert world.created == [] and calls == []


def test_not_a_tty(tmp_path, monkeypatch):
    (tmp_path / "bots" / "proxmox").mkdir(parents=True)
    (tmp_path / "bots" / "proxmox" / ".env.example").write_text(EXAMPLE)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "argv", ["wizard", str(tmp_path), "proxmox"])
    assert wizard.main() == 2


def test_abort_writes_nothing(tmp_path, monkeypatch):
    world = FakeWorld()
    monkeypatch.setattr(wizard, "http", world.http)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda prompt="": (_ for _ in ()).throw(KeyboardInterrupt))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    (tmp_path / "bots" / "proxmox").mkdir(parents=True)
    (tmp_path / "bots" / "proxmox" / ".env.example").write_text(EXAMPLE)
    monkeypatch.setattr(sys, "argv", ["wizard", str(tmp_path), "proxmox"])
    assert wizard.main() == 1
    assert not (tmp_path / "bots" / "proxmox" / ".env").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
