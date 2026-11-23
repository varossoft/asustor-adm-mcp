"""Logic for an Asustor NAS (ADM).

No MCP dependencies: plain functions that read /proc, /sys, the file
system (confined to allowed roots) and the Docker API through a
docker-socket-proxy. Everything is read-only except `restart_container`,
which is disabled by default (ALLOW_WRITE) and limited to an explicit
allowlist of container names (WRITE_ALLOWLIST).
"""

from __future__ import annotations

import glob
import os
import re
import struct
import subprocess
import threading
import time
from dataclasses import dataclass

import httpx

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")
# Block device (disk, partition or md array) -> its base device: sda1 -> sda, nvme0n1p4 -> nvme0n1.
BASE_DEV_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|md\d+)(?:p?\d+)?$")
SECRET_RE = re.compile(
    r"(?i)\b([\w.-]*(?:token|passw(?:or)?d|secret|api[_-]?key|authorization)"
    r"[\w.-]*\s*[=:]\s*)(?:Bearer\s+)?\S+"
)


# --------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------
def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _pairs(name: str, default: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _csv(name, default):
        if "=" in item:
            key, value = item.split("=", 1)
            out[key.strip()] = value.strip()
    return out


@dataclass
class Config:
    file_roots: list[str]
    storage_paths: dict[str, str]
    md_watch: set[str]
    md_labels: dict[str, str]
    nas_conf: str
    docker_host: str
    hidden_names: set[str]
    max_log_chars: int
    write_enabled: bool = False
    write_allowlist: frozenset[str] = frozenset()
    docker_write_host: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            file_roots=_csv("FILE_ROOTS"),
            storage_paths=_pairs("STORAGE_PATHS"),
            md_watch=set(_csv("MD_WATCH", "md1")),
            md_labels=_pairs("MD_LABELS"),
            nas_conf=os.getenv("NAS_CONF", "/host/nas.conf"),
            docker_host=os.getenv("DOCKER_HOST", "http://docker-socket-proxy:2375"),
            hidden_names=set(_csv("HIDDEN_NAMES", "#Recycle,@eaDir,@tmp")),
            max_log_chars=int(os.getenv("MAX_LOG_CHARS", "16000")),
            write_enabled=os.getenv("ALLOW_WRITE", "0").strip().lower() in ("1", "true", "yes"),
            write_allowlist=frozenset(_csv("WRITE_ALLOWLIST")),
            docker_write_host=os.getenv("DOCKER_WRITE_HOST", "").strip(),
        )


cfg = Config.from_env()

# Injectable in tests (httpx.MockTransport).
_transport: httpx.BaseTransport | None = None


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------
def read_text(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def human(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError("Invalid name (only letters, digits, _ . -)")
    return name


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1***", text)


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
def get_system_info() -> dict:
    conf: dict[str, str] = {}
    for line in read_text(cfg.nas_conf).splitlines():
        m = re.match(r"^\s*(Model|Version)\s*=\s*(.+?)\s*$", line)
        if m and m.group(1) not in conf:
            conf[m.group(1)] = m.group(2)

    cpuinfo = read_text("/proc/cpuinfo")
    cpu = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    cores = len(re.findall(r"^processor\s*:", cpuinfo, re.M))

    mem: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        m = re.match(r"^(\w+):\s+(\d+) kB", line)
        if m:
            mem[m.group(1)] = int(m.group(2)) * 1024

    up = read_text("/proc/uptime").split()
    load = read_text("/proc/loadavg").split()[:3]
    total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    swap_t, swap_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)

    return {
        "model": conf.get("Model"),
        "adm_version": conf.get("Version"),
        "kernel": os.uname().release,
        "arch": os.uname().machine,
        "cpu": cpu.group(1) if cpu else None,
        "cores": cores,
        "load_avg": load,
        "uptime_hours": round(float(up[0]) / 3600, 1) if up else None,
        "ram": {
            "total": human(total),
            "available": human(avail),
            "used_pct": round(100 * (total - avail) / total, 1) if total else None,
        },
        "swap": {
            "total": human(swap_t),
            "used": human(swap_t - swap_f),
            "used_pct": round(100 * (swap_t - swap_f) / swap_t, 1) if swap_t else None,
        },
    }


PROC_MOUNTS = "/proc/mounts"


def base_device(name: str) -> str | None:
    """sda1 -> sda, nvme0n1p4 -> nvme0n1, md1 -> md1 (None if not a known device)."""
    m = BASE_DEV_RE.match(name or "")
    return m.group(1) if m else None


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def parse_mounts(text: str) -> list[dict]:
    """Parse /proc/mounts. `device` is the name under /dev (sda1, md1...) or None."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 3:
            continue
        source = _unescape_mount(f[0])
        out.append(
            {
                "device": source[len("/dev/"):] if source.startswith("/dev/") else None,
                "mountpoint": _unescape_mount(f[1]),
                "fstype": f[2],
            }
        )
    return out


def find_mount(path: str, mounts: list[dict]) -> dict | None:
    """The mount that contains `path` (longest matching mountpoint)."""
    real = os.path.realpath(path)
    best = None
    for m in mounts:
        mp = m["mountpoint"]
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best["mountpoint"]):
                best = m
    return best


SYS_BLOCK = "/sys/block"


def _disk_serial(base: str) -> str | None:
    """Disk serial number. NVMe exposes it in device/serial; SATA drives, in
    VPD page 0x80 (4-byte header + ASCII serial)."""
    serial = read_text(f"{base}/device/serial").strip()
    if serial:
        return serial
    try:
        with open(f"{base}/device/vpd_pg80", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if len(raw) > 4:
        length = raw[3] or len(raw) - 4
        serial = raw[4 : 4 + length].decode("ascii", errors="ignore").strip("\x00 \r\n")
        return serial or None
    return None


def _disk_temps(base: str) -> dict:
    """Temperature from hwmon (NVMe and SATA with drivetemp), unprivileged."""
    for hw in sorted(glob.glob(f"{base}/device/hwmon*")):
        try:
            cur = int(read_text(f"{hw}/temp1_input").strip()) / 1000
        except ValueError:
            continue
        out = {"temp_c": round(cur, 1)}
        for key, name in (("temp1_max", "temp_max_c"), ("temp1_crit", "temp_crit_c")):
            try:
                out[name] = round(int(read_text(f"{hw}/{key}").strip()) / 1000, 1)
            except ValueError:
                pass
        return out
    return {}


def _raid_membership() -> dict[str, list[str]]:
    """Base disk -> names of the md arrays that use one of its partitions."""
    out: dict[str, list[str]] = {}
    for a in parse_mdstat(read_text("/proc/mdstat"), set())["arrays"]:
        for member in a["members"]:
            disk = base_device(member)
            if disk and a["name"] not in out.setdefault(disk, []):
                out[disk].append(a["name"])
    return out


def list_disks() -> list[dict]:
    """Physical disks from /sys/block. Includes the serial (device names change
    when disks are removed or added, the serial does not), the mount points of
    its partitions and the md arrays it belongs to, both read from the kernel."""
    mounts = parse_mounts(read_text(PROC_MOUNTS))
    raid = _raid_membership()
    disks = []
    try:
        names = sorted(os.listdir(SYS_BLOCK))
    except OSError:
        return disks
    for name in names:
        if not DISK_RE.match(name):
            continue
        base = f"{SYS_BLOCK}/{name}"
        try:
            size = int(read_text(f"{base}/size").strip() or 0) * 512
        except ValueError:
            size = 0
        rotational = read_text(f"{base}/queue/rotational").strip() == "1"
        kind = "NVMe" if name.startswith("nvme") else ("HDD" if rotational else "SSD")
        disks.append(
            {
                "device": name,
                "type": kind,
                "model": read_text(f"{base}/device/model").strip() or None,
                "serial": _disk_serial(base),
                "size": human(size),
                "size_bytes": size,
                "mounts": sorted(
                    m["mountpoint"] for m in mounts if m["device"] and base_device(m["device"]) == name
                ),
                "raid_arrays": sorted(raid.get(name, [])),
                **_disk_temps(base),
            }
        )
    return disks


def get_storage_usage() -> list[dict]:
    """Space per configured path, with the device and file system backing it."""
    out = []
    mounts = parse_mounts(read_text(PROC_MOUNTS))
    for label, path in cfg.storage_paths.items():
        try:
            st = os.statvfs(path)
        except OSError as exc:
            out.append({"name": label, "path": path, "error": str(exc)})
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        mount = find_mount(path, mounts) or {}
        out.append(
            {
                "name": label,
                "device": mount.get("device"),
                "fstype": mount.get("fstype"),
                "total": human(total),
                "used": human(used),
                "free": human(free),
                "used_pct": round(100 * used / total, 1) if total else None,
            }
        )
    return out


def parse_mdstat(text: str, watch: set[str], labels: dict[str, str] | None = None) -> dict:
    arrays: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"^(md\d+)\s*:\s*(.*)$", line)
        if m:
            rest = m.group(2)
            level = re.search(r"\b(raid\d+|linear)\b", rest)
            cur = {
                "name": m.group(1),
                "state": rest.split()[0] if rest.split() else None,
                "level": level.group(1) if level else None,
                "members": re.findall(r"([\w-]+)\[\d+\]", rest),
                "watched": m.group(1) in watch,
            }
            if labels and m.group(1) in labels:
                cur["label"] = labels[m.group(1)]
            arrays.append(cur)
            continue
        if cur is None:
            continue
        s = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", line)
        if s:
            cur["expected"], cur["active"] = int(s.group(1)), int(s.group(2))
            cur["status"] = s.group(3)
            cur["degraded"] = "_" in s.group(3)
        p = re.search(r"\b(resync|recovery|reshape|check)\s*=\s*([\d.]+)%", line)
        if p:
            cur["progress"] = f"{p.group(1)} {p.group(2)}%"
    for a in arrays:
        a["alert"] = bool(a["watched"] and a.get("degraded"))
    return {"alert": any(a["alert"] for a in arrays), "arrays": arrays}


def get_raid_status() -> dict:
    return parse_mdstat(read_text("/proc/mdstat"), cfg.md_watch, cfg.md_labels)


# --------------------------------------------------------------------------
# Files (read-only, confined to FILE_ROOTS)
# --------------------------------------------------------------------------
def _roots() -> list[str]:
    return [os.path.realpath(r) for r in cfg.file_roots if os.path.isdir(r)]


def safe_path(path: str) -> str:
    if not path or not path.startswith("/"):
        raise ValueError("Path must be absolute")
    real = os.path.realpath(path)
    for root in _roots():
        if real == root or real.startswith(root + os.sep):
            return real
    raise ValueError("Path outside the allowed roots")


def _hidden(name: str) -> bool:
    return name in cfg.hidden_names or name[:1] in (".", "@", "#")


def list_directory(path: str = "", limit: int = 200) -> dict:
    limit = max(1, min(limit, 1000))
    if not path:
        return {"roots": _roots()}
    real = safe_path(path)
    entries = []
    with os.scandir(real) as it:
        for e in it:
            if _hidden(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            entries.append(
                {
                    "name": e.name,
                    "type": "dir" if is_dir else "file",
                    "size": None if is_dir else human(st.st_size),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                }
            )
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return {
        "path": real,
        "total": len(entries),
        "truncated": len(entries) > limit,
        "entries": entries[:limit],
    }


def search_files(query: str, path: str = "", limit: int = 50, timeout: int = 20) -> dict:
    """Name search (substring, case-insensitive). Limited in results and time
    so the disks are not hammered."""
    if not query or len(query) > 100 or "\x00" in query:
        raise ValueError("Invalid query (1-100 characters)")
    limit = max(1, min(limit, 200))
    timeout = max(1, min(timeout, 60))
    bases = [safe_path(path)] if path else _roots()
    if not bases:
        raise ValueError("No roots configured (FILE_ROOTS)")

    pattern = "*" + re.sub(r"([\\*?\[\]])", r"\\\1", query) + "*"
    prune: list[str] = []
    for n in sorted(cfg.hidden_names):
        prune += ["-name", n, "-o"]
    cmd = ["find", *bases, "(", *prune[:-1], ")", "-prune", "-o", "-iname", pattern, "-print"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    results: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            results.append(line.rstrip("\n"))
            if len(results) >= limit:
                proc.kill()
                break
    finally:
        timer.cancel()
        proc.wait()
    return {
        "query": query,
        "count": len(results),
        "truncated": len(results) >= limit,
        "timed_out": timed_out.is_set(),
        "results": results,
    }


# --------------------------------------------------------------------------
# Docker (Engine API through docker-socket-proxy, GET only)
# --------------------------------------------------------------------------
def _get(endpoint: str, **params):
    try:
        with httpx.Client(base_url=cfg.docker_host, timeout=15, transport=_transport) as c:
            r = c.get(endpoint, params=params or None)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Docker ({cfg.docker_host}): {exc}") from exc
    if r.status_code == 403:
        raise RuntimeError("The docker-socket-proxy rejects this endpoint (403)")
    if r.status_code == 404:
        raise RuntimeError("Not found")
    r.raise_for_status()
    return r


def _ports(ports: list[dict]) -> list[str]:
    out = []
    for p in ports or []:
        base = f"{p.get('PrivatePort')}/{p.get('Type')}"
        out.append(f"{p['PublicPort']}->{base}" if p.get("PublicPort") else base)
    return sorted(set(out))


def list_containers(all: bool = True) -> list[dict]:
    items = _get("/containers/json", all=1 if all else 0).json()
    return [
        {
            "name": (i["Names"][0] if i.get("Names") else i["Id"][:12]).lstrip("/"),
            "image": i.get("Image"),
            "state": i.get("State"),
            "status": i.get("Status"),
            "ports": _ports(i.get("Ports", [])),
            "project": (i.get("Labels") or {}).get("com.docker.compose.project"),
        }
        for i in sorted(items, key=lambda x: (x.get("Names") or [""])[0])
    ]


def list_compose_projects() -> list[dict]:
    projects: dict[str, dict] = {}
    for c in list_containers(all=True):
        name = c["project"] or "(no compose project)"
        p = projects.setdefault(name, {"project": name, "running": 0, "total": 0})
        p["total"] += 1
        p["running"] += 1 if c["state"] == "running" else 0
    return sorted(projects.values(), key=lambda p: p["project"])


def inspect_container(name: str) -> dict:
    d = _get(f"/containers/{check_name(name)}/json").json()
    st, cfg_ = d.get("State", {}), d.get("Config", {})
    return {
        "name": d.get("Name", "").lstrip("/"),
        "image": cfg_.get("Image"),
        "state": st.get("Status"),
        "started_at": st.get("StartedAt"),
        "exit_code": st.get("ExitCode"),
        "oom_killed": st.get("OOMKilled"),
        "health": (st.get("Health") or {}).get("Status"),
        "restart_policy": d.get("HostConfig", {}).get("RestartPolicy", {}).get("Name"),
        "restart_count": d.get("RestartCount"),
        "network_mode": d.get("HostConfig", {}).get("NetworkMode"),
        "networks": sorted((d.get("NetworkSettings", {}).get("Networks") or {}).keys()),
        "mounts": [
            {"src": m.get("Source"), "dst": m.get("Destination"), "rw": m.get("RW")}
            for m in d.get("Mounts", [])
        ],
        # Only the variable NAMES: the values may be secrets.
        "env_names": sorted(e.split("=", 1)[0] for e in (cfg_.get("Env") or [])),
    }


def demux_docker_logs(data: bytes) -> str:
    """Logs from containers without a TTY come in frames with an 8-byte header."""
    if len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\x00\x00\x00":
        chunks, i = [], 0
        while i + 8 <= len(data):
            size = struct.unpack(">I", data[i + 4 : i + 8])[0]
            chunks.append(data[i + 8 : i + 8 + size])
            i += 8 + size
        data = b"".join(chunks)
    return data.decode("utf-8", errors="replace")


def get_container_logs(name: str, lines: int = 100) -> dict:
    lines = max(1, min(lines, 1000))
    r = _get(
        f"/containers/{check_name(name)}/logs",
        stdout=1, stderr=1, tail=lines, timestamps=1,
    )
    text = redact(demux_docker_logs(r.content))
    truncated = len(text) > cfg.max_log_chars
    return {
        "container": name,
        "truncated": truncated,
        "logs": text[-cfg.max_log_chars :] if truncated else text,
    }


def list_images() -> list[dict]:
    items = _get("/images/json").json()
    return [
        {"tags": i.get("RepoTags") or ["<none>"], "size": human(i.get("Size", 0))}
        for i in items
    ]


def list_networks() -> list[dict]:
    items = _get("/networks").json()
    return [
        {
            "name": n.get("Name"),
            "driver": n.get("Driver"),
            "subnets": [c.get("Subnet") for c in (n.get("IPAM") or {}).get("Config") or []],
        }
        for n in sorted(items, key=lambda x: x.get("Name", ""))
    ]


# --------------------------------------------------------------------------
# Write actions (disabled by default; separate docker-socket-proxy)
# --------------------------------------------------------------------------
def validate_write_config(c: Config | None = None) -> None:
    """Fail fast on an unsafe or incomplete write configuration."""
    c = c or cfg
    if not c.write_enabled:
        return
    if not c.write_allowlist:
        raise ValueError("ALLOW_WRITE is set but WRITE_ALLOWLIST is empty")
    bad = [n for n in c.write_allowlist if not NAME_RE.match(n)]
    if bad:
        raise ValueError(f"Invalid names in WRITE_ALLOWLIST: {sorted(bad)}")
    if not c.docker_write_host:
        raise ValueError("ALLOW_WRITE is set but DOCKER_WRITE_HOST is empty")
    if c.docker_write_host == c.docker_host:
        raise ValueError(
            "DOCKER_WRITE_HOST must be a different docker-socket-proxy than "
            "DOCKER_HOST so the read-only proxy keeps POST=0"
        )


def restart_container(name: str) -> dict:
    """Restart ONE container from the owner's allowlist (WRITE_ALLOWLIST).

    Only available when the server runs with ALLOW_WRITE=1. Any other
    container is refused, regardless of what the Docker proxy would allow.
    """
    name = check_name(name)
    if not cfg.write_enabled:
        raise PermissionError("Write mode is disabled (ALLOW_WRITE is not set)")
    if name not in cfg.write_allowlist:
        raise PermissionError(f"'{name}' is not in WRITE_ALLOWLIST")
    if not cfg.docker_write_host:
        raise RuntimeError("DOCKER_WRITE_HOST is not configured")
    try:
        with httpx.Client(base_url=cfg.docker_write_host, timeout=40, transport=_transport) as c:
            r = c.post(f"/containers/{name}/restart", params={"t": 10})
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Docker ({cfg.docker_write_host}): {exc}") from exc
    if r.status_code == 404:
        raise RuntimeError(f"Container '{name}' not found")
    if r.status_code == 403:
        raise RuntimeError("The write docker-socket-proxy rejects this endpoint (403)")
    r.raise_for_status()
    return {"container": name, "action": "restart", "status": "restarted"}
