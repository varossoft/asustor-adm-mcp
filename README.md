# asustor-adm-mcp

**Read-only by default** [MCP](https://modelcontextprotocol.io) server for Asustor NAS (ADM 5.x). It lets an assistant (Claude Desktop, Claude Code, Cursor…) query the NAS state without you having to paste command output by hand. An optional, opt-in write mode adds a single action (`restart_container`) limited to an allowlist you define.

Tested on an AS5402T running ADM 5.1.4. Built for ADM: it does not use `lsblk`, `smartctl` or any Synology API; it reads `/proc`, `/sys`, `/etc/nas.conf` and the Docker API.

## Tools (12 read-only, +1 opt-in write)

| Tool | What it returns |
|---|---|
| `get_system_info` | Model and ADM version, kernel, CPU, load, uptime, RAM and swap |
| `list_disks` | Physical disks (`sdX`, `nvmeXnY`): model, **serial** (NVMe and SATA), type, size, **temperature** (hwmon, if the kernel exposes it), **mount points** of their partitions and the **md arrays** they belong to |
| `get_storage_usage` | Usage of the paths configured in `STORAGE_PATHS`, with the backing device and file system |
| `get_raid_status` | Arrays from `/proc/mdstat`, missing members, rebuild progress and an alert on watched arrays |
| `list_directory`, `search_files` | Names and metadata inside `FILE_ROOTS` (name search with result and time limits). **Never reads file contents.** |
| `list_containers`, `list_compose_projects`, `inspect_container`, `get_container_logs`, `list_images`, `list_networks` | Docker state. `inspect_container` returns only the *names* of environment variables; `get_container_logs` masks typical secret patterns |

| `restart_container` | **Write mode only** (`ALLOW_WRITE=1`). Restarts one container whose exact name is in `WRITE_ALLOWLIST`; anything else is refused before it reaches Docker |

Naming convention: `list_*` for collections, `get_*` for a single report, `inspect_*` for one object, `search_*` for searches.

## Giving the assistant context

The data the MCP returns does not explain your architecture, and a model tends to fill in the gaps (for example, suggesting loose data disks be used to rebuild a RAID). Describe your NAS in `NAS_NOTES`: it is delivered to the client as the server instructions. The MCP is read-only unless you enable write mode, but you should always double-check the model's suggestions before acting on them.

## Security

- **No writes by default.** With the default configuration there are no tools that stop, start or delete anything.
- **Isolated Docker socket.** Only the `docker-socket-proxy` containers mount it; the MCP talks to them over HTTP on `internal` networks. The read proxy always runs with `POST=0`.
- **Mandatory authentication.** Bearer token (≥ 24 characters); the server will not start without it. Only `/health` is public.
- **Confined paths.** `realpath` against `FILE_ROOTS`: no escaping with `..` or symlinks. Hidden by default: `#Recycle`, `@eaDir`, `@tmp` and anything starting with `.`, `@` or `#`.
- **Hardened container.** Non-root user, read-only filesystem, `cap_drop: ALL`, `no-new-privileges`.
- **What NOT to mount.** Do not expose `/volume1/Docker` or folders containing `.env` files, databases or keys. An empty folder is used to measure `volume1`.
- **Do not publish it to the internet.** Use it on the LAN or over a VPN (Tailscale, WireGuard). If you put it behind a reverse proxy, keep the token.

### Write mode (optional)

Off by default. It adds one tool, `restart_container`, and refuses to start unless it is fully configured (`ALLOW_WRITE=1`, a non-empty `WRITE_ALLOWLIST` of valid names, and `DOCKER_WRITE_HOST` pointing to a *different* proxy than `DOCKER_HOST`).

- **Separate write proxy.** `docker-socket-proxy-write` (compose profile `write`, `POST=1`, `CONTAINERS=1`, `ALLOW_RESTARTS=1`) lives on its own `internal` network that only the MCP joins. The read proxy keeps `POST=0`.
- **Understand what that proxy permits.** docker-socket-proxy cannot filter by container name, and with `POST=1` + `CONTAINERS=1` it also accepts creating containers, which is equivalent to root on the host. `ALLOW_RESTARTS` also covers stop/kill. The allowlist is therefore enforced in this server's code (exact name match, validated characters, tests that nothing outside the list reaches Docker), and `restart_container` only ever sends `POST /containers/<name>/restart`. A bug in this server is the residual risk: keep the token secret and the port on LAN/VPN only.
- **Choose the allowlist carefully.** Stateless apps only. Do not include your DNS/reverse proxy/password manager, databases, VPN containers other containers share a network with, or the MCP itself.

```sh
# .env
ALLOW_WRITE=1
WRITE_ALLOWLIST=sonarr,radarr,lazylibrarian
DOCKER_WRITE_HOST=http://docker-socket-proxy-write:2375
docker compose --profile write up -d --build
```

## Deployment

```sh
git clone https://github.com/varossoft/asustor-adm-mcp.git mcp-nas && cd mcp-nas
cp .env.example .env            # edit MCP_TOKEN (openssl rand -hex 32) and paths
mkdir -p /volume1/Docker/mcp-nas/vol1
docker compose up -d --build
```

In `.env`, set `MCP_BIND_IP` to the NAS LAN (or Tailscale) IP: by default it only listens on `127.0.0.1`.

The compose file attaches the MCP to an external Docker network called `proxy` (useful if you publish it through a reverse proxy); if you do not have one, remove it from the compose file or create it with `docker network create proxy`.

Endpoint: `http://<host>:8088/mcp` (streamable HTTP) with the header `Authorization: Bearer <MCP_TOKEN>`.

Example for Claude Code:

```sh
claude mcp add --transport http nas http://<NAS-IP>:8088/mcp --header "Authorization: Bearer $MCP_TOKEN"
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_TOKEN` | — (required) | Bearer token |
| `FILE_ROOTS` | empty | Paths that can be listed/searched |
| `STORAGE_PATHS` | empty | `label=path,...` for disk usage |
| `MD_LABELS` | empty | `array=label,...` so `get_raid_status` shows which volume each array belongs to |
| `ALLOW_WRITE` | `0` | `1` enables write mode (see above) |
| `WRITE_ALLOWLIST` | empty | Exact container names `restart_container` may restart (comma-separated) |
| `DOCKER_WRITE_HOST` | empty | URL of the write docker-socket-proxy; must differ from `DOCKER_HOST` |
| `NAS_NOTES` | empty | Free text the server sends to the client as instructions: describe your architecture (what each volume is, which disks are not RAID...) so the assistant does not assume it. Single line; use `\n` for line breaks |
| `MCP_BIND_IP` | `127.0.0.1` | Host IP on which port 8088 is published (LAN or Tailscale). Do not use `0.0.0.0` |
| `MD_WATCH` | `md1` | Arrays that raise an alert when degraded. On ADM, `md0` and other system mirrors always show missing members (`[4/1]`) and should not be watched |
| `HIDDEN_NAMES` | `#Recycle,@eaDir,@tmp` | Hidden folders |
| `MAX_LOG_CHARS` | `16000` | Character limit returned by `get_container_logs` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8088` | Server listen address |

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest
```

The tests include real `/proc/mdstat` output from a degraded RAID1 volume.

## Changelog

### Unreleased
- Opt-in write mode: `restart_container` (allowlist, separate `POST=1` proxy under the `write` compose profile, fail-fast config validation). Disabled by default; the default behaviour and the 12 read-only tools are unchanged.

### 0.3.0
- **Breaking:** tools renamed to a consistent `verb_noun` scheme: `get_system_overview` → `get_system_info`, `get_disks` → `list_disks`, `list_files` → `list_directory`, `docker_ps` → `list_containers`, `docker_projects` → `list_compose_projects`, `docker_inspect` → `inspect_container`, `docker_logs` → `get_container_logs`, `docker_images` → `list_images`, `docker_networks` → `list_networks`. The server name is now `asustor-adm-mcp`.
- `list_disks` reports the mount points and md arrays of each disk, read from `/proc/mounts` and `/proc/mdstat`, so the assistant no longer depends on `NAS_NOTES` for that.
- `get_storage_usage` reports the backing `device` and `fstype`.

## Roadmap

- SMART: deliberately ruled out for now. Reading it from a container requires elevated capabilities (`SYS_ADMIN` for NVMe, `SYS_RAWIO` for SATA) and device access, too much privilege for a read-only service. Temperature is read without privileges.
- Btrfs volume status.

## License

[MIT](LICENSE).
