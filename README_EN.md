# Ubuntu System Manager MCP Server

[Türkçe](README.md)

A **fully read-only** server that collects general status information about
an Ubuntu server (version, resource usage, apt updates, service health,
security summary, logs, hardware) and exposes it to AI clients (Claude
Desktop, Claude Code, Open WebUI) as MCP tools. It never makes any change to
the system.

## Tools

| Tool | Scope |
|---|---|
| `sistem_ozeti` | Ubuntu version, kernel, uptime, CPU, physical/virtual |
| `kaynak_kullanimi` | Load average, RAM/swap, disk/inode usage |
| `wan_ip_getir` | External (WAN) IP address |
| `guncelleme_durumu` | Upgradable packages, security update status, ESM, unattended-upgrades history |
| `servis_saglik` | Failed systemd services + status of specified services |
| `guvenlik_ozeti` | Failed SSH logins (24h), last 20 successful SSH logins, active sessions |
| `log_ozeti` | Kernel + syslog warnings/errors via journalctl (24h, max 100 lines) |

## Architecture: why it's built this way

This server is built around reading the host's state **without modifying
it**, and **without requiring root** wherever possible. The container is
never granted write access or broad capabilities; for each data source only
the relevant host file/directory is bind-mounted read-only (`:ro`).

- **Disk/inode usage** — instead of mounting the host's entire root
  filesystem (which would open up a huge attack surface), an **empty
  "probe" directory** is created on each partition you want to monitor, and
  only that empty directory is mounted. `statvfs()` reports the filesystem a
  path lives on, not the path's contents — so even an empty directory
  yields the real disk usage of its partition. The container never sees any
  of the host's actual files.
- **Apt update status** — the container **never runs its own**
  `apt-get update` (that would require root/network access and would be
  slow on every call). Instead, the host's `apt-daily.timer` (or a cron job)
  refreshes the cache periodically; the container mounts
  `/var/lib/apt/lists` and `/var/lib/dpkg/status` read-only and parses them
  directly with `python-debian`. The cache's age (`cache_son_guncelleme`)
  is included in the response so that stale data isn't silently returned if
  the host-side timer ever breaks.
- **Service health** — read-only queries like `systemctl --failed` /
  `is-active` work by connecting to the host's D-Bus system bus socket
  (`/run/dbus/system_bus_socket`). Under systemd's default polkit policy,
  read-only unit queries generally don't require authentication (only
  start/stop/restart do) — root shouldn't be needed, but this behavior can
  vary across distributions and should be verified on the target system.
- **Security summary** — the `ufw`/`iptables` open-port summary was
  deliberately left **out of scope**: `ufw status` refuses outright for a
  non-root user and effectively requires a broad capability like
  `--cap-add=NET_ADMIN`, which conflicts with the "minimal privilege" goal.
  Successful logins are also parsed from the `Accepted ...` lines in
  `auth.log` rather than from `last`/wtmp — on some distributions (e.g.
  Debian trixie) the `last` command has moved out of classic `util-linux`
  into a separate package (`wtmpdb`), which would have been both an extra
  dependency and an assumption that the host still uses the classic wtmp
  format.
- **Logs** — **`journalctl -k`** is used instead of raw `dmesg`. Direct
  `dmesg`/`/dev/kmsg` access is gated not by file permissions but by the
  `kernel.dmesg_restrict` sysctl, requiring the `CAP_SYSLOG` capability (or
  root); `journalctl` exposes the same information with just
  `systemd-journal` group membership — no extra capability needed.
- **Hardware (physical/virtual)** — running `systemd-detect-virt` inside the
  container detects **the container itself, not the host** (it will likely
  report "docker"). Instead, `/sys/class/dmi/id/*` files are read directly
  and compared against known VM vendor signatures (KVM, VMware, VirtualBox,
  GCE, AWS, ...).
- **Network** — the local interface list/routing table was deliberately left
  out of scope (recon/information-disclosure risk, especially if the server
  is exposed to the WAN).

Net result: `docker run` needs no `--cap-add` at all — the container runs
with Docker's default (dropped) capability set. The only requirements are
the read-only mounts and group memberships needed to reach the host's D-Bus
and specific log/config files.

## Setup

### 1. Host-side preparation

**Find the group IDs** (so the container can read log files without being root):

```bash
getent group adm systemd-journal utmp
```

Copy `.env.example` to `.env` and update the GIDs:

```bash
cp .env.example .env
```

**Create disk probe directories** — an empty directory on each mount point
you want to monitor (see the "Architecture" section above):

```bash
sudo mkdir -p /.mcp-disk-probe
# for additional mount points, e.g.:
# sudo mkdir -p /home/.mcp-disk-probe
# sudo mkdir -p /var/.mcp-disk-probe
```

For each extra probe directory, add/uncomment the corresponding line in
`compose.yaml`'s `volumes` list.

**Keep the apt cache fresh** — the container doesn't run its own
`apt-get update`, so set the host's `apt-daily.timer` to run hourly:

```bash
sudo systemctl edit apt-daily.timer
```

add to the file that opens:

```ini
[Timer]
OnCalendar=
OnCalendar=hourly
```

save, then:

```bash
sudo systemctl restart apt-daily.timer
```

### 2. Build & run

```bash
docker compose build
docker compose up -d
```

The `cache_son_guncelleme` field in the `guncelleme_durumu` tool's response
shows how fresh the apt cache is — if this value keeps getting older, check
`apt-daily.timer`.

### 3. Connect an AI client

**Claude Desktop / Claude Code (stdio):**

```json
{
  "mcpServers": {
    "ubuntu-system-summary": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/mcp-ubuntu-system-summary/compose.yaml",
        "run", "--rm", "-T", "ubuntu-system-summary"
      ]
    }
  }
}
```

**Open WebUI (streamable-http):** set `TRANSPORT=streamable-http` in `.env`,
uncomment the `ports` block in `compose.yaml` (preferably binding only to an
intranet/WireGuard interface), then after `docker compose up -d` point Open
WebUI at `http://<host>:8000/mcp`.

## Known limitations / things to verify

- **Ubuntu Pro/ESM status** (`guncelleme_durumu.pro_esm_durumu`) — whether
  the `pro` command returns a cached status without root, or simply refuses
  and returns `kullanılamıyor`, has not been tested on a target system.
- **Service health** (`servis_saglik`) — whether read-only `systemctl`
  queries can reach the host's D-Bus from inside the container without root
  has not been tested on a target system; polkit policy can differ across
  distributions.
- **The `apt list --upgradable` comparison** doesn't apply apt's
  pinning/priority rules — it simply picks the highest version seen. On
  systems with unusual pinning setups, this may diverge from apt's actual
  behavior.
- **Security update age** (`cache_yasi_gun`) shows how old the local cache
  of the relevant `-security` repo is — it is not the original release date
  of any specific package.
- **On 22.04+ minimal installs** the rsyslog package may be absent and
  `/var/log/auth.log` may never be created — in that case SSH login data is
  automatically read from the `journalctl -t sshd` fallback instead.

## Security notes

- `auth.log`/syslog content can contain sensitive data such as IP addresses
  and usernames. If you expose this server to the WAN (e.g. via Open WebUI),
  either drop these tools or restrict access behind a VPN/intranet layer.
- The container runs with `read_only: true`, `cap_drop: [ALL]`, and
  `no-new-privileges`; all mounts are `:ro`.

## License

[MIT](LICENSE)
