"""Lógica de solo lectura para un NAS Asustor (ADM).

Sin dependencias de MCP: son funciones puras que leen /proc, /sys, el
sistema de archivos (confinado a raíces permitidas) y la API de Docker a
través de un docker-socket-proxy. Nada escribe ni ejecuta acciones.
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
SECRET_RE = re.compile(
    r"(?i)\b([\w.-]*(?:token|passw(?:or)?d|secret|api[_-]?key|authorization)"
    r"[\w.-]*\s*[=:]\s*)(?:Bearer\s+)?\S+"
)


# --------------------------------------------------------------------------
# Configuración (variables de entorno)
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
        )


cfg = Config.from_env()

# Inyectable en tests (httpx.MockTransport).
_transport: httpx.BaseTransport | None = None


# --------------------------------------------------------------------------
# Utilidades
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
        raise ValueError("Nombre no válido (solo letras, números, _ . -)")
    return name


def redact(text: str) -> str:
    return SECRET_RE.sub(r"\1***", text)


# --------------------------------------------------------------------------
# Sistema
# --------------------------------------------------------------------------
def get_system_overview() -> dict:
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


SYS_BLOCK = "/sys/block"


def _disk_serial(base: str) -> str | None:
    """Serie del disco. NVMe la expone en device/serial; los SATA, en la
    página VPD 0x80 (4 bytes de cabecera + serie ASCII)."""
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
    """Temperatura desde hwmon (NVMe y SATA con drivetemp), sin privilegios."""
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


def get_disks() -> list[dict]:
    """Discos físicos desde /sys/block. Incluye serie: los nombres cambian
    al quitar o añadir discos, la serie no."""
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
                **_disk_temps(base),
            }
        )
    return disks


def get_storage_usage() -> list[dict]:
    out = []
    for label, path in cfg.storage_paths.items():
        try:
            st = os.statvfs(path)
        except OSError as exc:
            out.append({"name": label, "path": path, "error": str(exc)})
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        out.append(
            {
                "name": label,
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
# Archivos (solo lectura, confinados a FILE_ROOTS)
# --------------------------------------------------------------------------
def _roots() -> list[str]:
    return [os.path.realpath(r) for r in cfg.file_roots if os.path.isdir(r)]


def safe_path(path: str) -> str:
    if not path or not path.startswith("/"):
        raise ValueError("La ruta debe ser absoluta")
    real = os.path.realpath(path)
    for root in _roots():
        if real == root or real.startswith(root + os.sep):
            return real
    raise ValueError("Ruta fuera de las raíces permitidas")


def _hidden(name: str) -> bool:
    return name in cfg.hidden_names or name[:1] in (".", "@", "#")


def list_files(path: str = "", limit: int = 200) -> dict:
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
    """Búsqueda por nombre (subcadena, sin distinguir mayúsculas). Con límite
    de resultados y de tiempo para no machacar los discos."""
    if not query or len(query) > 100 or "\x00" in query:
        raise ValueError("Consulta no válida (1-100 caracteres)")
    limit = max(1, min(limit, 200))
    timeout = max(1, min(timeout, 60))
    bases = [safe_path(path)] if path else _roots()
    if not bases:
        raise ValueError("No hay raíces configuradas (FILE_ROOTS)")

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
# Docker (API de Engine a través de docker-socket-proxy, solo GET)
# --------------------------------------------------------------------------
def _get(endpoint: str, **params):
    try:
        with httpx.Client(base_url=cfg.docker_host, timeout=15, transport=_transport) as c:
            r = c.get(endpoint, params=params or None)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"No se pudo contactar con Docker ({cfg.docker_host}): {exc}") from exc
    if r.status_code == 403:
        raise RuntimeError("El docker-socket-proxy rechaza este endpoint (403)")
    if r.status_code == 404:
        raise RuntimeError("No encontrado")
    r.raise_for_status()
    return r


def _ports(ports: list[dict]) -> list[str]:
    out = []
    for p in ports or []:
        base = f"{p.get('PrivatePort')}/{p.get('Type')}"
        out.append(f"{p['PublicPort']}->{base}" if p.get("PublicPort") else base)
    return sorted(set(out))


def docker_ps(all: bool = True) -> list[dict]:
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


def docker_projects() -> list[dict]:
    projects: dict[str, dict] = {}
    for c in docker_ps(all=True):
        name = c["project"] or "(sin proyecto compose)"
        p = projects.setdefault(name, {"project": name, "running": 0, "total": 0})
        p["total"] += 1
        p["running"] += 1 if c["state"] == "running" else 0
    return sorted(projects.values(), key=lambda p: p["project"])


def docker_inspect(name: str) -> dict:
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
        # Solo los NOMBRES de las variables: los valores pueden ser secretos.
        "env_names": sorted(e.split("=", 1)[0] for e in (cfg_.get("Env") or [])),
    }


def demux_docker_logs(data: bytes) -> str:
    """Los logs de contenedores sin TTY vienen en tramas de 8 bytes de cabecera."""
    if len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\x00\x00\x00":
        chunks, i = [], 0
        while i + 8 <= len(data):
            size = struct.unpack(">I", data[i + 4 : i + 8])[0]
            chunks.append(data[i + 8 : i + 8 + size])
            i += 8 + size
        data = b"".join(chunks)
    return data.decode("utf-8", errors="replace")


def docker_logs(name: str, lines: int = 100) -> dict:
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


def docker_images() -> list[dict]:
    items = _get("/images/json").json()
    return [
        {"tags": i.get("RepoTags") or ["<none>"], "size": human(i.get("Size", 0))}
        for i in items
    ]


def docker_networks() -> list[dict]:
    items = _get("/networks").json()
    return [
        {
            "name": n.get("Name"),
            "driver": n.get("Driver"),
            "subnets": [c.get("Subnet") for c in (n.get("IPAM") or {}).get("Config") or []],
        }
        for n in sorted(items, key=lambda x: x.get("Name", ""))
    ]
