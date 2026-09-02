from periscope_unifi.util import (
    client_counts,
    client_line,
    client_link,
    client_signal,
    device_clients,
    device_cpu,
    device_line,
    device_temp,
    device_type,
    event_line,
    find_client,
    find_device,
    is_mac,
    normalize_mac,
    status_dot_for,
    wan_summary,
)

HEALTH = [
    {"subsystem": "wan", "status": "ok", "wan_ip": "203.0.113.5", "gw_name": "Dream Machine",
     "rx_bytes-r": 1_500_000, "tx_bytes-r": 200_000, "gw_system-stats": {"uptime": "99"}},
    {"subsystem": "www", "status": "ok", "latency": 12, "uptime": 86400, "isp_name": "Example ISP"},
    {"subsystem": "lan", "status": "ok", "num_user": 20, "num_guest": 0, "num_iot": 3},
    {"subsystem": "wlan", "status": "warning", "num_user": 10, "num_guest": 2, "num_iot": 0},
]

DEVICES = [
    {"mac": "aa:aa:aa:aa:aa:01", "name": "Core Switch", "model": "USW-24-PoE", "type": "usw", "state": 1,
     "system-stats": {"cpu": "12.5", "mem": "40"}, "general_temperature": 47, "num_sta": 8, "upgradable": True},
    {"mac": "aa:aa:aa:aa:aa:02", "name": "Garage AP", "model": "U6-LR", "type": "uap", "state": 0,
     "temperatures": [{"name": "cpu", "value": 55.2}, {"name": "phy", "value": 60.1}], "user-num_sta": 3},
]

CLIENTS = [
    {"mac": "bb:bb:bb:bb:bb:01", "name": "Laptop", "hostname": "zj-laptop", "ip": "10.0.0.5", "is_wired": False,
     "essid": "LabNet", "ap_mac": "aa:aa:aa:aa:aa:02", "signal": -61, "uptime": 3600,
     "rx_bytes": 2048, "tx_bytes": 1024},
    {"mac": "bb:bb:bb:bb:bb:02", "hostname": "nas", "ip": "10.0.0.10", "is_wired": True,
     "sw_mac": "aa:aa:aa:aa:aa:01", "sw_port": 5, "rssi": 0},
]
BY_MAC = {d["mac"]: d for d in DEVICES}


def test_mac_normalisation():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("AABBCCDDEEFF") == "aa:bb:cc:dd:ee:ff"
    assert is_mac("aa:bb:cc:dd:ee:ff") and not is_mac("laptop")


def test_find_client_by_mac_name_hostname():
    assert find_client(CLIENTS, "BB-BB-BB-BB-BB-02")["hostname"] == "nas"
    assert find_client(CLIENTS, "laptop")["mac"] == "bb:bb:bb:bb:bb:01"
    assert find_client(CLIENTS, "zj-lap")["mac"] == "bb:bb:bb:bb:bb:01"
    assert find_client(CLIENTS, "nope") is None


def test_client_formatting():
    assert client_link(CLIENTS[0], BY_MAC) == "📶 LabNet via Garage AP"
    assert client_link(CLIENTS[1], BY_MAC) == "🔌 Core Switch port 5"
    assert client_signal(CLIENTS[0]) == "-61 dBm"
    assert client_signal(CLIENTS[1]) == ""
    line = client_line(CLIENTS[0], BY_MAC)
    assert line.startswith("**Laptop** `10.0.0.5` `bb:bb:bb:bb:bb:01`")
    assert "up 1h" in line and "↓2.0 KB ↑1.0 KB" in line


def test_device_helpers():
    sw, ap = DEVICES
    assert device_type(sw) == "Switch" and device_type(ap) == "Access Point"
    assert device_cpu(sw) == 12.5 and device_cpu(ap) is None
    assert device_temp(sw) == 47 and device_temp(ap) == 60.1
    assert device_clients(sw) == 8 and device_clients(ap) == 3
    assert device_line(sw) == "🟢 **Core Switch** (USW-24-PoE) · cpu 12% · 47°C · 8 clients · ⬆ fw"
    assert device_line(ap).startswith("🔴 **Garage AP** (U6-LR)")
    assert find_device(DEVICES, "garage")["mac"] == "aa:aa:aa:aa:aa:02"
    assert find_device(DEVICES, "AA:AA:AA:AA:AA:01")["name"] == "Core Switch"


def test_wan_summary_and_counts():
    w = wan_summary(HEALTH)
    assert w.present and w.ok is True
    assert w.ip == "203.0.113.5" and w.latency_ms == 12 and w.uptime_s == 86400
    assert w.isp == "Example ISP" and w.rx_bps == 1_500_000
    c = client_counts(HEALTH)
    assert c == {"wired": 20, "wireless": 10, "guest": 2, "iot": 3, "total": 35}
    assert status_dot_for("warning") == "🟡" and status_dot_for(None) == "⚪"


def test_wan_down_variants():
    down = [dict(HEALTH[0], status="error"), HEALTH[1]]
    assert wan_summary(down).ok is False
    internet_down = [HEALTH[0], dict(HEALTH[1], status="error")]
    assert wan_summary(internet_down).ok is False and wan_summary(internet_down).status == "error"
    no_gateway = [HEALTH[2], HEALTH[3]]
    w = wan_summary(no_gateway)
    assert w.present is False and w.ok is None


def test_event_line():
    assert event_line({"time": 1_700_000_000_000, "msg": "AP[Garage AP] was restarted"}) == \
        "<t:1700000000:R> AP[Garage AP] was restarted"
    assert event_line({"key": "EVT_AP_Restarted"}) == "EVT_AP_Restarted"
