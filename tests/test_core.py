import json
import os
import struct

import httpx
import pytest

from adm_mcp import core

# Real /proc/mdstat output from an AS5402T with a single NVMe (degraded RAID1).
MDSTAT_DEGRADED = """Personalities : [linear] [raid0] [raid1] [raid10] [raid6] [raid5] [raid4]
md1 : active raid1 nvme0n1p4[0]
      972173312 blocks super 1.2 [2/1] [U_]

md126 : active raid1 nvme0n1p3[0]
      2094080 blocks super 1.2 [4/1] [U___]

md0 : active raid1 nvme0n1p2[0]
      2094080 blocks super 1.2 [4/1] [U___]

unused devices: <none>
"""

MDSTAT_REBUILD = """md1 : active raid1 nvme1n1p4[2] nvme0n1p4[0]
      972173312 blocks super 1.2 [2/1] [U_]
      [>....................]  recovery =  2.0% (19500000/972173312) finish=88.2min speed=180000K/sec
"""


@pytest.fixture(autouse=True)
def _cfg(tmp_path):
    (tmp_path / "data" / "docs").mkdir(parents=True)
    (tmp_path / "data" / "docs" / "Invoice Acme.pdf").write_text("x")
    (tmp_path / "data" / "#Recycle").mkdir()
    (tmp_path / "data" / "#Recycle" / "deleted invoice.pdf").write_text("x")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("x")
    os.symlink(tmp_path / "outside", tmp_path / "data" / "escape")
    core.cfg.file_roots = [str(tmp_path / "data")]
    core.cfg.md_watch = {"md1"}
    yield


def test_mdstat_alerts_only_on_watched_array():
    r = core.parse_mdstat(MDSTAT_DEGRADED, {"md1"})
    by = {a["name"]: a for a in r["arrays"]}
    assert r["alert"] is True
    assert by["md1"]["alert"] and by["md1"]["status"] == "U_" and by["md1"]["members"] == ["nvme0n1p4"]
    assert by["md0"]["degraded"] and not by["md0"]["alert"]
    assert by["md126"]["degraded"] and not by["md126"]["alert"]


def test_mdstat_healthy_no_alert():
    ok = MDSTAT_DEGRADED.replace("[2/1] [U_]", "[2/2] [UU]")
    assert core.parse_mdstat(ok, {"md1"})["alert"] is False


def test_mdstat_rebuild_progress():
    r = core.parse_mdstat(MDSTAT_REBUILD, {"md1"})
    assert r["arrays"][0]["progress"] == "recovery 2.0%"
    assert r["arrays"][0]["members"] == ["nvme1n1p4", "nvme0n1p4"]


def test_safe_path_blocks_traversal_and_symlink_escape(tmp_path):
    root = str(tmp_path / "data")
    assert core.safe_path(root + "/docs").endswith("docs")
    for bad in ("", "docs", "/etc/passwd", root + "/../outside", root + "/escape/secret.txt"):
        with pytest.raises(ValueError):
            core.safe_path(bad)


def test_list_files_hides_recycle(tmp_path):
    r = core.list_files(str(tmp_path / "data"))
    names = {e["name"] for e in r["entries"]}
    assert "docs" in names and "#Recycle" not in names


def test_search_files_name_only_and_skips_recycle():
    r = core.search_files("invoice")
    assert r["count"] == 1 and r["results"][0].endswith("Invoice Acme.pdf")


def test_search_files_escapes_glob_chars():
    assert core.search_files("*")["count"] == 0
    assert core.search_files("[a-z]")["count"] == 0


def test_search_rejects_bad_query():
    for q in ("", "x" * 101):
        with pytest.raises(ValueError):
            core.search_files(q)


def test_check_name():
    assert core.check_name("sonarr-anime")
    for bad in ("--rm", "-x", "a b", "a/b", ""):
        with pytest.raises(ValueError):
            core.check_name(bad)


def _frame(stream: int, payload: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def test_demux_and_raw_logs():
    framed = _frame(1, b"hello\n") + _frame(2, b"error\n")
    assert core.demux_docker_logs(framed) == "hello\nerror\n"
    assert core.demux_docker_logs(b"plain tty text\n") == "plain tty text\n"


def test_redact():
    out = core.redact("api_key=abc123 password: hunter2 Authorization: Bearer xyz normal")
    assert "abc123" not in out and "hunter2" not in out and "xyz" not in out
    assert "normal" in out


def _mock_docker(handler):
    core._transport = httpx.MockTransport(handler)
    core.cfg.docker_host = "http://proxy:2375"


def teardown_function():
    core._transport = None


def test_docker_ps_and_projects():
    containers = [
        {"Names": ["/sonarr"], "Image": "lscr.io/linuxserver/sonarr", "State": "running",
         "Status": "Up 2 days", "Ports": [{"PrivatePort": 8989, "PublicPort": 8989, "Type": "tcp"}],
         "Labels": {"com.docker.compose.project": "servarr"}},
        {"Names": ["/radarr"], "Image": "x", "State": "exited", "Status": "Exited", "Ports": [],
         "Labels": {"com.docker.compose.project": "servarr"}},
    ]
    _mock_docker(lambda req: httpx.Response(200, json=containers))
    ps = core.docker_ps()
    assert [c["name"] for c in ps] == ["radarr", "sonarr"]
    assert core.docker_projects() == [{"project": "servarr", "running": 1, "total": 2}]


def test_docker_inspect_hides_env_values():
    body = {"Name": "/n8n", "Config": {"Image": "n8n", "Env": ["DB_PASSWORD=supersecret", "TZ=UTC"]},
            "State": {"Status": "running"}, "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
            "NetworkSettings": {"Networks": {"proxy": {}}}, "Mounts": []}
    _mock_docker(lambda req: httpx.Response(200, json=body))
    r = core.docker_inspect("n8n")
    assert r["env_names"] == ["DB_PASSWORD", "TZ"]
    assert "supersecret" not in json.dumps(r)


def test_docker_logs_redacts_and_truncates():
    payload = _frame(1, b"token=abc123 startup\n" + b"x" * 100)
    _mock_docker(lambda req: httpx.Response(200, content=payload))
    core.cfg.max_log_chars = 50
    r = core.docker_logs("n8n", lines=10)
    assert r["truncated"] and len(r["logs"]) == 50
    core.cfg.max_log_chars = 16000
    r = core.docker_logs("n8n", lines=10)
    assert "abc123" not in r["logs"]


def test_docker_proxy_403_message():
    _mock_docker(lambda req: httpx.Response(403))
    with pytest.raises(RuntimeError, match="rejects"):
        core.docker_images()


def test_system_overview_parses_nas_conf(tmp_path):
    conf = tmp_path / "nas.conf"
    conf.write_text("[General]\nModel = AS5402T\nVersion = 5.1.4.RM62\nLastversion = 5.1.3\nModelName = AS5402T\n")
    core.cfg.nas_conf = str(conf)
    r = core.get_system_overview()
    assert r["model"] == "AS5402T" and r["adm_version"] == "5.1.4.RM62"


def _fake_sysfs(tmp_path):
    root = tmp_path / "sys_block"
    nvme = root / "nvme0n1"
    (nvme / "queue").mkdir(parents=True)
    (nvme / "device" / "hwmon1").mkdir(parents=True)
    (nvme / "size").write_text("1953525168\n")
    (nvme / "queue" / "rotational").write_text("0\n")
    (nvme / "device" / "model").write_text("CT1000P310SSD8\n")
    (nvme / "device" / "serial").write_text("NVME-SERIAL-1 \n")
    (nvme / "device" / "hwmon1" / "temp1_input").write_text("39850\n")
    (nvme / "device" / "hwmon1" / "temp1_max").write_text("84850\n")
    (nvme / "device" / "hwmon1" / "temp1_crit").write_text("89900\n")

    sda = root / "sda"
    (sda / "queue").mkdir(parents=True)
    (sda / "device").mkdir(parents=True)
    (sda / "size").write_text("11721045168\n")
    (sda / "queue" / "rotational").write_text("1\n")
    (sda / "device" / "model").write_text("ST6000VN006-2ZM1\n")
    # VPD page 0x80: 4-byte header (the last byte is the length) + ASCII serial.
    (sda / "device" / "vpd_pg80").write_bytes(b"\x00\x80\x00\x08" + b"ZXA12345" + b"\x00" * 8)

    (root / "md1").mkdir()          # not a physical disk: must be ignored
    (root / "loop0").mkdir()
    return root


def test_get_disks_serial_and_temperature(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "SYS_BLOCK", str(_fake_sysfs(tmp_path)))
    disks = {d["device"]: d for d in core.get_disks()}
    assert set(disks) == {"nvme0n1", "sda"}
    assert disks["nvme0n1"]["serial"] == "NVME-SERIAL-1"
    assert disks["nvme0n1"]["temp_c"] == 39.9 and disks["nvme0n1"]["temp_crit_c"] == 89.9
    assert disks["nvme0n1"]["type"] == "NVMe"
    assert disks["sda"]["serial"] == "ZXA12345" and disks["sda"]["type"] == "HDD"
    assert "temp_c" not in disks["sda"]


def test_disk_serial_missing_is_none(tmp_path):
    (tmp_path / "device").mkdir()
    assert core._disk_serial(str(tmp_path)) is None


def test_md_labels():
    r = core.parse_mdstat(MDSTAT_DEGRADED, {"md1"}, {"md1": "volume1", "md0": "system"})
    by = {a["name"]: a for a in r["arrays"]}
    assert by["md1"]["label"] == "volume1" and by["md0"]["label"] == "system"
    assert "label" not in by["md126"]
