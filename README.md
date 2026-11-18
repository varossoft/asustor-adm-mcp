# asustor-adm-mcp

Servidor [MCP](https://modelcontextprotocol.io) **de solo lectura** para NAS Asustor (ADM 5.x). Permite que un asistente (Claude Desktop, Claude Code, Cursor…) consulte el estado del NAS sin tener que pegar a mano la salida de comandos.

Probado en un AS5402T con ADM 5.1.4. Pensado para ADM: no usa `lsblk`, `smartctl` ni ninguna API de Synology; lee `/proc`, `/sys`, `/etc/nas.conf` y la API de Docker.

## Herramientas (12, todas de solo lectura)

| Tool | Qué devuelve |
|---|---|
| `get_system_overview` | Modelo y versión de ADM, kernel, CPU, carga, uptime, RAM y swap |
| `get_disks` | Discos físicos (`sdX`, `nvmeXnY`): modelo, **serie** (NVMe y SATA), tipo, tamaño y **temperatura** (hwmon, si el kernel la expone) |
| `get_storage_usage` | Uso de las rutas configuradas en `STORAGE_PATHS` |
| `get_raid_status` | Arrays de `/proc/mdstat`, huecos, progreso de reconstrucción y alerta en los arrays vigilados |
| `list_files`, `search_files` | Nombres y metadatos dentro de `FILE_ROOTS` (búsqueda por nombre con límite de resultados y de tiempo). **No lee contenido.** |
| `docker_ps`, `docker_projects`, `docker_inspect`, `docker_logs`, `docker_images`, `docker_networks` | Estado de Docker. `inspect` devuelve solo los *nombres* de las variables de entorno; `logs` enmascara patrones típicos de secretos |

## Dar contexto al asistente

Los datos que devuelve el MCP no explican tu arquitectura, y un modelo tiende a rellenar los huecos (por ejemplo, sugerir usar discos de datos sueltos para reconstruir un RAID). Describe tu NAS en `NAS_NOTES`: se entrega al cliente en las instrucciones del servidor. El MCP es de solo lectura, pero las sugerencias del modelo conviene comprobarlas siempre antes de actuar.

## Seguridad

- **Sin escritura.** No hay tools que paren, arranquen o borren nada.
- **Socket de Docker aislado.** Solo el contenedor `docker-socket-proxy` lo monta, con `POST=0`; el MCP habla con él por HTTP en una red `internal`.
- **Autenticación obligatoria.** Bearer token (≥ 24 caracteres); el servidor no arranca sin él. Solo `/health` es público.
- **Rutas confinadas.** `realpath` contra `FILE_ROOTS`: no se escapa con `..` ni con enlaces simbólicos. Ocultas por defecto: `#Recycle`, `@eaDir`, `@tmp` y todo lo que empiece por `.`, `@` o `#`.
- **Contenedor endurecido.** Usuario no root, sistema de archivos de solo lectura, `cap_drop: ALL`, `no-new-privileges`.
- **Qué NO montar.** No expongas `/volume1/Docker` ni carpetas con `.env`, bases de datos o claves. Para medir `volume1` se usa una carpeta vacía.
- **No lo publiques en internet.** Úsalo en la LAN o por VPN (Tailscale, WireGuard). Si lo pones detrás de un proxy inverso, mantén el token.

## Despliegue

```sh
git clone https://github.com/varossoft/asustor-adm-mcp.git mcp-nas && cd mcp-nas
cp .env.example .env            # edita MCP_TOKEN (openssl rand -hex 32) y rutas
mkdir -p /volume1/Docker/mcp-nas/vol1
docker compose up -d --build
```

Edita en `.env` `MCP_BIND_IP` con la IP LAN (o de Tailscale) del NAS: por defecto solo escucha en `127.0.0.1`.

El compose une el MCP a una red Docker externa llamada `proxy` (útil si lo publicas con un proxy inverso); si no la tienes, elimínala del compose o créala con `docker network create proxy`.

Endpoint: `http://<host>:8088/mcp` (streamable HTTP) con cabecera `Authorization: Bearer <MCP_TOKEN>`.

Ejemplo para Claude Code:

```sh
claude mcp add --transport http nas http://<IP-DEL-NAS>:8088/mcp --header "Authorization: Bearer $MCP_TOKEN"
```

## Configuración

| Variable | Por defecto | Descripción |
|---|---|---|
| `MCP_TOKEN` | — (obligatoria) | Bearer token |
| `FILE_ROOTS` | vacío | Rutas listables/buscables |
| `STORAGE_PATHS` | vacío | `etiqueta=ruta,...` para el uso de disco |
| `MD_LABELS` | vacío | `array=etiqueta,...` para que `get_raid_status` indique a qué volumen corresponde cada array |
| `NAS_NOTES` | vacío | Texto libre que el servidor envía al cliente como instrucciones: describe tu arquitectura (qué es cada volumen, qué discos no son RAID...) para que el asistente no la suponga. Una sola línea; `\n` para saltos |
| `MCP_BIND_IP` | `127.0.0.1` | IP del host en la que se publica el puerto 8088 (LAN o Tailscale). No uses `0.0.0.0` |
| `MD_WATCH` | `md1` | Arrays que generan alerta si están degradados. En ADM, `md0` y otros espejos de sistema aparecen siempre con huecos (`[4/1]`) y no deberían vigilarse |
| `HIDDEN_NAMES` | `#Recycle,@eaDir,@tmp` | Carpetas ocultas |
| `MAX_LOG_CHARS` | `16000` | Límite de caracteres devueltos por `docker_logs` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8088` | Escucha del servidor |

## Desarrollo

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest
```

Los tests incluyen una salida real de `/proc/mdstat` de un volumen RAID1 degradado.

## Hoja de ruta

- SMART: descartado por ahora a propósito. Leerlo desde un contenedor exige capacidades elevadas (`SYS_ADMIN` para NVMe, `SYS_RAWIO` para SATA) y acceso a dispositivos, demasiado privilegio para un servicio de solo lectura. La temperatura sí se lee sin privilegios.
- Estado de volúmenes Btrfs.

## Licencia

[MIT](LICENSE).
