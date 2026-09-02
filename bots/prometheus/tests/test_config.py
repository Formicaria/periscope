import pytest

from periscope_prometheus.config import PromSettings


def _base(monkeypatch):
    monkeypatch.setenv("PROM_URL", "http://prom:9090/")
    monkeypatch.setenv("ALERTMANAGER_URL", "http://am:9093")
    for k in ("GRAFANA_URL", "GRAFANA_TOKEN", "PROM_BASIC_USER", "PROM_BASIC_PASS", "VERIFY_SSL",
              "PROM_TARGET_WATCH", "GRAFANA_RENDER_WIDTH", "GRAFANA_DEFAULT_DASHBOARD_UID"):
        monkeypatch.delenv(k, raising=False)


def test_defaults(monkeypatch):
    _base(monkeypatch)
    s = PromSettings.from_env()
    assert s.prom_url == "http://prom:9090"
    assert s.alertmanager_url == "http://am:9093"
    assert s.grafana_enabled is False
    assert s.verify_ssl is True and s.target_watch is True
    assert (s.render_width, s.render_height, s.grafana_org_id) == (1000, 500, 1)


def test_full(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GRAFANA_URL", "https://grafana.lab/")
    monkeypatch.setenv("GRAFANA_TOKEN", "glsa_x")
    monkeypatch.setenv("GRAFANA_ORG_ID", "3")
    monkeypatch.setenv("PROM_BASIC_USER", "u")
    monkeypatch.setenv("PROM_BASIC_PASS", "p")
    monkeypatch.setenv("VERIFY_SSL", "false")
    monkeypatch.setenv("PROM_TARGET_WATCH", "0")
    monkeypatch.setenv("GRAFANA_RENDER_WIDTH", "800")
    monkeypatch.setenv("GRAFANA_DEFAULT_DASHBOARD_UID", "abc")
    s = PromSettings.from_env()
    assert s.grafana_url == "https://grafana.lab" and s.grafana_enabled
    assert s.grafana_org_id == 3 and s.render_width == 800
    assert s.verify_ssl is False and s.target_watch is False
    assert s.default_dashboard_uid == "abc"


def test_missing_required(monkeypatch):
    _base(monkeypatch)
    monkeypatch.delenv("PROM_URL")
    with pytest.raises(RuntimeError, match="PROM_URL"):
        PromSettings.from_env()


def test_grafana_needs_token(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GRAFANA_URL", "http://g:3000")
    with pytest.raises(RuntimeError, match="GRAFANA_TOKEN"):
        PromSettings.from_env()


def test_basic_auth_pair(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("PROM_BASIC_USER", "u")
    with pytest.raises(RuntimeError, match="PROM_BASIC"):
        PromSettings.from_env()


def test_bad_scheme(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("PROM_URL", "prom:9090")
    with pytest.raises(RuntimeError, match="http"):
        PromSettings.from_env()
