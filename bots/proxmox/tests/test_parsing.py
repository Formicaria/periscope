from periscope_proxmox.client import parse_resources, parse_upid, summarize_vzdump_log, task_ok
from periscope_proxmox.cogs.status import build_board
from periscope_proxmox.cogs.storage import storage_embed
from periscope_proxmox.cogs.tasks import task_line
from periscope_proxmox.cogs.vms import guest_detail, guest_line, guest_pages
from periscope_proxmox.config import PveSettings

GiB = 1024 ** 3

RESOURCES = [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.12, "maxcpu": 16, "mem": 20 * GiB,
     "maxmem": 64 * GiB, "uptime": 90000},
    {"type": "node", "node": "pve2", "status": "offline", "cpu": 0, "maxcpu": 8, "mem": 0, "maxmem": 32 * GiB},
    {"type": "qemu", "vmid": 100, "name": "web", "node": "pve1", "status": "running", "cpu": 0.5, "maxcpu": 4,
     "mem": 2 * GiB, "maxmem": 4 * GiB, "maxdisk": 32 * GiB, "uptime": 3600},
    {"type": "qemu", "vmid": 9000, "name": "tpl-debian", "node": "pve1", "status": "stopped", "template": 1},
    {"type": "lxc", "vmid": 101, "name": "pihole", "node": "pve1", "status": "stopped", "maxmem": GiB},
    {"type": "storage", "storage": "local", "node": "pve1", "status": "available", "disk": 50 * GiB,
     "maxdisk": 100 * GiB, "shared": 0},
    {"type": "storage", "storage": "nfs", "node": "pve1", "status": "available", "disk": 96 * GiB,
     "maxdisk": 100 * GiB, "shared": 1},
    {"type": "storage", "storage": "nfs", "node": "pve2", "status": "unknown", "disk": 96 * GiB,
     "maxdisk": 100 * GiB, "shared": 1},
]

CFG = PveSettings(url="https://pve.local:8006", token_id="a@pam!b", token_secret="s")


def test_parse_resources():
    snap = parse_resources(RESOURCES)
    assert [n.name for n in snap.nodes] == ["pve1", "pve2"]
    n1 = snap.node("pve1")
    assert n1.online and abs(n1.cpu_pct - 12.0) < 1e-6 and abs(n1.mem_pct - 31.25) < 1e-6
    assert not snap.node("pve2").online
    assert [g.vmid for g in snap.guests] == [100, 101, 9000]
    web = snap.guest(100)
    assert web.kind == "qemu" and web.running and web.label == "VM" and web.node == "pve1"
    assert snap.guest(101).label == "CT" and not snap.guest(101).running
    assert snap.guest(9000).template is True
    assert len(snap.guests_on("pve1")) == 3
    # shared storage collapsed to one entry keyed by name; local keyed by node/name
    keys = [s.key for s in snap.unique_storages()]
    assert keys == ["nfs", "pve1/local"]
    assert abs(snap.unique_storages()[0].pct - 96.0) < 1e-6


def test_parse_upid():
    upid = "UPID:pve1:0001A2B3:0123ABCD:64F1C2A0:vzdump:100:root@pam:"
    d = parse_upid(upid)
    assert d["node"] == "pve1" and d["type"] == "vzdump" and d["id"] == "100" and d["user"] == "root@pam"
    assert d["starttime"] == 0x64F1C2A0 and d["pid"] == 0x1A2B3
    assert parse_upid("garbage")["type"] == "?"


def test_task_ok():
    assert task_ok({"status": "OK", "endtime": 1}) is True
    assert task_ok({"status": "job errors", "endtime": 1}) is False
    assert task_ok({"upid": "x"}) is None


def test_summarize_vzdump_log():
    lines = [
        "INFO: starting new backup job: vzdump 100 101",
        "INFO: Starting Backup of VM 100 (qemu)",
        "INFO: archive file size: 1.23GB",
        "INFO: Finished Backup of VM 100 (00:01:23)",
        "INFO: Starting Backup of VM 101 (lxc)",
        "ERROR: Backup of VM 101 failed - no space left on device",
        "INFO: Backup job finished with errors",
    ]
    s = summarize_vzdump_log(lines)
    assert s["ok"] == [{"vmid": "100", "duration": "00:01:23", "size": "1.23GB"}]
    assert s["failed"] == [{"vmid": "101", "reason": "no space left on device"}]


def test_status_board_embed():
    snap = parse_resources(RESOURCES)
    e = build_board(snap, CFG, lab_name="lab1", cluster="homelab", active_alerts=2)
    assert "homelab" in e.title
    assert "1/2" in e.description and "1/2** guests" in e.description  # templates excluded
    assert "2 active alerts" in e.description
    names = [f.name for f in e.fields]
    assert names[:2] == ["🟢 pve1", "🔴 pve2"] and names[-1] == "Storage"
    storage = e.fields[-1].value
    assert "🔴 **nfs**" in storage and "🟢 **pve1/local**" in storage
    assert "lab1" in e.footer.text
    assert len(e) <= 6000


def test_guest_pages_and_lines():
    snap = parse_resources(RESOURCES)
    line = guest_line(snap.guest(100))
    assert "🟢" in line and "web" in line and "cpu 50%" in line
    assert "(template)" in guest_line(snap.guest(9000))
    many = [snap.guest(100)] * 23
    pages = guest_pages(many, lab_name="lab1", title="t")
    assert len(pages) == 3 and pages[2].author.name.endswith("page 3/3")
    assert guest_pages([], lab_name="lab1", title="t")[0].description == "No guests match."


def test_guest_detail_embed():
    snap = parse_resources(RESOURCES)
    cur = {"status": "running", "name": "web", "cpu": 0.25, "cpus": 4, "mem": 2 * GiB, "maxmem": 4 * GiB,
           "maxdisk": 32 * GiB, "uptime": 7200, "netin": 1024, "netout": 2048, "ha": {"managed": 0},
           "tags": "prod", "agent": 1}
    e = guest_detail(snap.guest(100), cur, lab_name="lab1", pve_url=CFG.url)
    assert e.title.endswith("VM 100 · web")
    assert e.url.startswith(CFG.url)
    vals = {f.name: f.value for f in e.fields}
    assert vals["CPU"].startswith("25.0%") and vals["Uptime"] == "2h" and "prod" in vals["Misc"]


def test_task_line_and_storage_embed():
    t = {"upid": "UPID:pve1:0001A2B3:0123ABCD:64F1C2A0:vzdump:100:root@pam:", "type": "vzdump", "id": "100",
         "node": "pve1", "user": "root@pam", "starttime": 1000, "endtime": 1090, "status": "job errors"}
    line = task_line(t)
    assert line.startswith("🔴 **vzdump** `100` · pve1 · root@pam") and "1m" in line and "job errors" in line
    rows = [
        {"storage": "local", "type": "dir", "active": 1, "used": 96 * GiB, "total": 100 * GiB, "avail": 4 * GiB,
         "content": "iso,backup"},
        {"storage": "broken", "type": "nfs", "active": 0},
    ]
    e = storage_embed("pve1", rows, CFG, lab_name="lab1")
    assert e.fields[0].name == "⚪ broken" and e.fields[1].name == "🔴 local"
    assert e.color.value == 0xE74C3C
