"""Wiring checks: every slash command lands under the single /unifi group and binds to its cog."""

from types import SimpleNamespace

from periscope_unifi.cogs import attach, unifi
from periscope_unifi.cogs.clients import ClientsCog
from periscope_unifi.cogs.devices import DevicesCog
from periscope_unifi.cogs.status import StatusCog  # noqa: F401  (import must succeed)

EXPECTED = {"clients", "client", "kick", "block", "unblock", "devices", "device", "restart", "wan", "events", "alarms"}


def test_group_has_every_command():
    assert {c.name for c in unifi.commands} == EXPECTED
    assert all(c.parent is unifi for c in unifi.commands)


def test_attach_binds_and_registers_once():
    added = []
    tree = SimpleNamespace(get_command=lambda name: unifi if added else None, add_command=added.append)
    bot = SimpleNamespace(tree=tree)
    clients = ClientsCog.__new__(ClientsCog)
    devices = DevicesCog.__new__(DevicesCog)
    attach(bot, clients)
    attach(bot, devices)
    assert added == [unifi]
    assert unifi.get_command("kick").binding is clients
    assert unifi.get_command("restart").binding is devices
    # autocomplete callbacks defined on the cog receive the binding too
    assert unifi.get_command("client")._params["mac_or_name"].autocomplete.pass_command_binding is True


def test_admin_commands_have_checks():
    for name in ("kick", "block", "unblock", "restart"):
        assert unifi.get_command(name).checks, f"/unifi {name} must be admin-gated"
