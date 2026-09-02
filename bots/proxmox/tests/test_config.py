import pytest

from periscope_proxmox.config import PveSettings

BASE = {
    "PVE_URL": "https://pve.local:8006/",
    "PVE_TOKEN_ID": "periscope@pam!discord",
    "PVE_TOKEN_SECRET": "00000000-0000-0000-0000-000000000000",
}


def _set(monkeypatch, **extra):
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET", "PVE_VERIFY_SSL", "PVE_CPU_WARN", "PVE_MEM_WARN",
              "PVE_STORAGE_WARN", "PVE_STORAGE_CRIT", "PVE_WATCH_BACKUPS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**BASE, **extra}.items():
        monkeypatch.setenv(k, v)


def test_defaults(monkeypatch):
    _set(monkeypatch)
    cfg = PveSettings.from_env()
    assert cfg.url == "https://pve.local:8006"  # trailing slash stripped
    assert cfg.token_id == "periscope@pam!discord"
    assert cfg.verify_ssl is False
    assert (cfg.cpu_warn, cfg.mem_warn, cfg.storage_warn, cfg.storage_crit) == (85, 90, 85, 95)
    assert cfg.watch_backups is True


def test_overrides(monkeypatch):
    _set(monkeypatch, PVE_VERIFY_SSL="true", PVE_CPU_WARN="70", PVE_STORAGE_WARN="80", PVE_STORAGE_CRIT="90",
         PVE_WATCH_BACKUPS="no")
    cfg = PveSettings.from_env()
    assert cfg.verify_ssl is True
    assert cfg.cpu_warn == 70
    assert cfg.storage_warn == 80 and cfg.storage_crit == 90
    assert cfg.watch_backups is False


@pytest.mark.parametrize("missing", ["PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET"])
def test_missing_required(monkeypatch, missing):
    _set(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        PveSettings.from_env()


def test_bad_values(monkeypatch):
    _set(monkeypatch, PVE_TOKEN_ID="justaname")
    with pytest.raises(RuntimeError, match="PVE_TOKEN_ID"):
        PveSettings.from_env()
    _set(monkeypatch, PVE_URL="pve.local:8006")
    with pytest.raises(RuntimeError, match="PVE_URL"):
        PveSettings.from_env()
    _set(monkeypatch, PVE_STORAGE_WARN="96", PVE_STORAGE_CRIT="95")
    with pytest.raises(RuntimeError, match="PVE_STORAGE_CRIT"):
        PveSettings.from_env()
    _set(monkeypatch, PVE_CPU_WARN="150")
    with pytest.raises(RuntimeError, match="PVE_CPU_WARN"):
        PveSettings.from_env()
