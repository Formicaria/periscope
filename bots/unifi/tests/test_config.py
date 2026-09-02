import pytest

from periscope_unifi.config import UnifiConfig


def test_defaults_unifi_os(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://192.168.1.1/")
    monkeypatch.setenv("UNIFI_USER", "periscope")
    monkeypatch.setenv("UNIFI_PASS", "secret")
    cfg = UnifiConfig.from_env()
    assert cfg.url == "https://192.168.1.1"
    assert cfg.site == "default"
    assert cfg.is_unifi_os is True
    assert cfg.verify_ssl is False
    assert cfg.alert_new_clients is True
    assert cfg.wan_latency_warn_ms == 100
    assert cfg.device_cpu_warn == 80
    assert cfg.known_clients_ttl_days == 30
    assert cfg.login_path == "/api/auth/login"
    assert cfg.site_path("stat/sta") == "/proxy/network/api/s/default/stat/sta"
    assert cfg.ttl_seconds == 30 * 86400


def test_self_hosted_controller(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "https://unifi.lab:8443")
    monkeypatch.setenv("UNIFI_USER", "u")
    monkeypatch.setenv("UNIFI_PASS", "p")
    monkeypatch.setenv("UNIFI_SITE", "home")
    monkeypatch.setenv("UNIFI_IS_UNIFI_OS", "false")
    monkeypatch.setenv("VERIFY_SSL", "true")
    monkeypatch.setenv("UNIFI_ALERT_NEW_CLIENTS", "no")
    monkeypatch.setenv("UNIFI_WAN_LATENCY_WARN_MS", "250")
    monkeypatch.setenv("UNIFI_DEVICE_CPU_WARN", "90")
    monkeypatch.setenv("UNIFI_KNOWN_CLIENTS_TTL_DAYS", "7")
    cfg = UnifiConfig.from_env()
    assert cfg.login_path == "/api/login"
    assert cfg.api_prefix == ""
    assert cfg.site_path("/stat/device") == "/api/s/home/stat/device"
    assert cfg.verify_ssl is True
    assert cfg.alert_new_clients is False
    assert (cfg.wan_latency_warn_ms, cfg.device_cpu_warn, cfg.known_clients_ttl_days) == (250, 90, 7)


def test_missing_required(monkeypatch):
    for k in ("UNIFI_URL", "UNIFI_USER", "UNIFI_PASS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("UNIFI_URL", "https://x")
    monkeypatch.setenv("UNIFI_USER", "u")
    with pytest.raises(RuntimeError, match="UNIFI_PASS"):
        UnifiConfig.from_env()


def test_bad_url_scheme(monkeypatch):
    monkeypatch.setenv("UNIFI_URL", "192.168.1.1")
    monkeypatch.setenv("UNIFI_USER", "u")
    monkeypatch.setenv("UNIFI_PASS", "p")
    with pytest.raises(RuntimeError, match="http"):
        UnifiConfig.from_env()
