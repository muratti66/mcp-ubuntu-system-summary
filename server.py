"""Ubuntu System Manager MCP Server — salt okunur sistem bilgisi tool'ları.
"""

import email.utils
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from debian.debian_support import Version
from debian.deb822 import Deb822
from mcp.server.mcpserver import MCPServer

OS_RELEASE = Path("/etc/os-release")
APT_LISTS_DIR = Path("/var/lib/apt/lists")
DPKG_STATUS = Path("/var/lib/dpkg/status")
UU_LOG_DIR = Path("/var/log/unattended-upgrades")
AUTH_LOG = Path("/var/log/auth.log")
UTMP = Path("/run/utmp")
DMI_DIR = Path("/sys/class/dmi/id")
DISK_PROBE_DIR = Path("/probe")

VM_VENDOR_HINTS = {
    "qemu": "KVM/QEMU",
    "kvm": "KVM/QEMU",
    "vmware": "VMware",
    "innotek": "VirtualBox",
    "virtualbox": "VirtualBox",
    "microsoft corporation": "Hyper-V",
    "google": "Google Compute Engine",
    "amazon ec2": "AWS EC2",
    "xen": "Xen",
    "digitalocean": "DigitalOcean",
}

mcp = MCPServer("ubuntu-system-summary")


def _run(args: list[str], timeout: int = 10) -> dict:
    """Salt-okunur bir komutu çalıştırır, hatayı da yapılandırılmış döner."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except FileNotFoundError:
        return {"hata": f"komut bulunamadı: {args[0]}"}
    except subprocess.TimeoutExpired:
        return {"hata": f"zaman aşımı: {' '.join(args)}"}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except (FileNotFoundError, PermissionError):
        return None


def _parse_os_release() -> dict:
    text = _read_text(OS_RELEASE)
    if text is None:
        return {"hata": "/etc/os-release okunamadı (mount edilmemiş olabilir)"}
    data = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key] = value.strip('"')
    return {
        "pretty_name": data.get("PRETTY_NAME"),
        "version_id": data.get("VERSION_ID"),
        "version_codename": data.get("VERSION_CODENAME"),
    }


def _parse_uptime() -> dict:
    text = _read_text(Path("/proc/uptime"))
    if text is None:
        return {"hata": "/proc/uptime okunamadı"}
    seconds = float(text.split()[0])
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return {
        "saniye": int(seconds),
        "okunabilir": f"{days} gün {hours} saat {minutes} dakika",
    }


def _parse_cpuinfo() -> dict:
    text = _read_text(Path("/proc/cpuinfo"))
    if text is None:
        return {"hata": "/proc/cpuinfo okunamadı"}
    model = None
    core_count = 0
    for line in text.splitlines():
        if line.startswith("model name") and model is None:
            model = line.split(":", 1)[1].strip()
        if line.startswith("processor"):
            core_count += 1
    return {"model": model, "mantiksal_cekirdek_sayisi": core_count}


def _detect_virt() -> dict:
    vendor = _read_text(DMI_DIR / "sys_vendor")
    product = _read_text(DMI_DIR / "product_name")
    if vendor is None and product is None:
        return {
            "durum": "belirlenemedi",
            "not": "/sys/class/dmi/id mount edilmemiş olabilir",
        }
    haystack = f"{vendor or ''} {product or ''}".lower()
    for hint, label in VM_VENDOR_HINTS.items():
        if hint in haystack:
            return {"durum": "sanal", "tip": label, "vendor": vendor, "product": product}
    return {
        "durum": "fiziksel (olası)",
        "vendor": vendor,
        "product": product,
        "not": "DMI imzasıyla eşleşme bulunamadı; kesin garanti değildir",
    }


@mcp.tool()
def sistem_ozeti() -> dict:
    """Ubuntu sürümü, kernel, uptime, CPU ve donanım (fiziksel/sanal) özeti."""
    uname = _run(["uname", "-srvmo"])
    return {
        "surum": _parse_os_release(),
        "kernel": uname.get("stdout") or uname.get("hata"),
        "uptime": _parse_uptime(),
        "cpu": _parse_cpuinfo(),
        "sanallastirma": _detect_virt(),
    }


def _parse_meminfo() -> dict:
    text = _read_text(Path("/proc/meminfo"))
    if text is None:
        return {"hata": "/proc/meminfo okunamadı"}
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip().split()
        if rest:
            values[key] = int(rest[0])  # kB cinsinden
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_free = values.get("SwapFree", 0)
    return {
        "ram_toplam_mb": total // 1024,
        "ram_kullanilabilir_mb": available // 1024,
        "ram_kullanilan_mb": (total - available) // 1024,
        "swap_toplam_mb": swap_total // 1024,
        "swap_kullanilan_mb": (swap_total - swap_free) // 1024,
    }


def _parse_loadavg() -> dict:
    text = _read_text(Path("/proc/loadavg"))
    if text is None:
        return {"hata": "/proc/loadavg okunamadı"}
    parts = text.split()
    return {"1dk": float(parts[0]), "5dk": float(parts[1]), "15dk": float(parts[2])}


def _disk_usage() -> list[dict]:
    if not DISK_PROBE_DIR.is_dir():
        return [{"hata": "/probe dizini mount edilmemiş — disk probe'ları yapılandırılmamış"}]
    sonuc = []
    for probe in sorted(DISK_PROBE_DIR.iterdir()):
        if not probe.is_dir():
            continue
        try:
            st = os.statvfs(probe)
        except OSError as exc:
            sonuc.append({"etiket": probe.name, "hata": str(exc)})
            continue
        total_bytes = st.f_frsize * st.f_blocks
        free_bytes = st.f_frsize * st.f_bavail
        used_bytes = total_bytes - (st.f_frsize * st.f_bfree)
        entry = {
            "etiket": probe.name,
            "toplam_gb": round(total_bytes / 1_000_000_000, 2),
            "kullanilan_gb": round(used_bytes / 1_000_000_000, 2),
            "bos_gb": round(free_bytes / 1_000_000_000, 2),
            "kullanim_yuzdesi": round(100 * used_bytes / total_bytes, 1) if total_bytes else None,
        }
        if st.f_files:
            inode_used = st.f_files - st.f_ffree
            entry["inode_toplam"] = st.f_files
            entry["inode_kullanim_yuzdesi"] = round(100 * inode_used / st.f_files, 1)
        sonuc.append(entry)
    return sonuc


@mcp.tool()
def kaynak_kullanimi() -> dict:
    """CPU load average, RAM/swap ve probe dizinleri üzerinden disk/inode kullanımı."""
    return {
        "load_average": _parse_loadavg(),
        "bellek": _parse_meminfo(),
        "disk": _disk_usage(),
    }


@mcp.tool()
def wan_ip_getir() -> dict:
    """Sunucunun dış (WAN) IP adresini birkaç fallback servisten sorgular."""
    import urllib.error
    import urllib.request

    servisler = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for url in servisler:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return {"wan_ip": ip, "kaynak": url}
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return {"hata": "hiçbir WAN IP servisine ulaşılamadı"}


def _iter_deb822(path: Path):
    try:
        with open(path, "rb") as f:
            yield from Deb822.iter_paragraphs(f)
    except (FileNotFoundError, PermissionError):
        return


def _installed_packages() -> dict[str, str]:
    installed = {}
    for stanza in _iter_deb822(DPKG_STATUS):
        status = stanza.get("Status", "")
        if not status.endswith("installed"):
            continue
        name = stanza.get("Package")
        arch = stanza.get("Architecture", "")
        version = stanza.get("Version")
        if name and version:
            installed[f"{name}:{arch}"] = version
    return installed


def _candidate_packages() -> tuple[dict[str, str], dict[str, str]]:
    """(paket -> en yüksek aday versiyon, paket -> onu içeren dosya adı) döner."""
    candidates: dict[str, str] = {}
    candidate_src: dict[str, str] = {}
    if not APT_LISTS_DIR.is_dir():
        return candidates, candidate_src
    for entry in APT_LISTS_DIR.iterdir():
        if not entry.name.endswith("_Packages"):
            continue
        for stanza in _iter_deb822(entry):
            name = stanza.get("Package")
            arch = stanza.get("Architecture", "")
            version = stanza.get("Version")
            if not name or not version:
                continue
            key = f"{name}:{arch}"
            current = candidates.get(key)
            if current is None or Version(version) > Version(current):
                candidates[key] = version
                candidate_src[key] = entry.name
    return candidates, candidate_src


def _security_update_age_days(upgradable_keys: list[str], candidate_src: dict[str, str]) -> dict:
    security_files = {
        candidate_src[k] for k in upgradable_keys if "security" in candidate_src.get(k, "")
    }
    if not security_files:
        return {"bekleyen_guvenlik_guncellemesi": False}
    oldest_date = None
    for fname in security_files:
        release_name = fname.rsplit("_", 1)[0] + "_Release"
        text = _read_text(APT_LISTS_DIR / release_name)
        if not text:
            continue
        for line in text.splitlines():
            if line.startswith("Date:"):
                try:
                    dt = email.utils.parsedate_to_datetime(line.split(":", 1)[1].strip())
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if oldest_date is None or dt < oldest_date:
                        oldest_date = dt
                except (ValueError, TypeError):
                    continue
    if oldest_date is None:
        return {"bekleyen_guvenlik_guncellemesi": True, "not": "Release tarihi okunamadı"}
    age = datetime.now(timezone.utc) - oldest_date
    return {
        "bekleyen_guvenlik_guncellemesi": True,
        "cache_yasi_gun": age.days,
        "not": "bu, güvenlik reposunun yerel cache yaşıdır; paketin ilk yayın tarihi değildir",
    }


def _unattended_upgrades_recent() -> list[str]:
    log_path = UU_LOG_DIR / "unattended-upgrades.log"
    text = _read_text(log_path)
    if text is None:
        return []
    cutoff = datetime.now() - timedelta(hours=24)
    satirlar = []
    for line in text.splitlines():
        if "Packages that will be upgraded" in line or "Packages that are upgraded" in line:
            try:
                ts = datetime.strptime(line.split()[0] + " " + line.split()[1], "%Y-%m-%d %H:%M:%S,%f")
            except (ValueError, IndexError):
                continue
            if ts >= cutoff:
                satirlar.append(line.strip())
    return satirlar


def _pro_status() -> dict:
    result = _run(["pro", "status", "--format=json"], timeout=15)
    if "hata" in result:
        return {"durum": "kullanılamıyor", "detay": result["hata"]}
    if result["returncode"] != 0:
        return {"durum": "kullanılamıyor", "detay": result["stderr"] or result["stdout"]}
    return {"durum": "ok", "ham_cikti": result["stdout"]}


@mcp.tool()
def guncelleme_durumu() -> dict:
    """Apt cache'inden (host cron ile tazelenen) yükseltilebilir paketler, ESM durumu
    ve son 24 saatte unattended-upgrades ile yüklenen paketler."""
    installed = _installed_packages()
    candidates, candidate_src = _candidate_packages()
    upgradable = []
    upgradable_keys = []
    for key, cur_version in installed.items():
        cand = candidates.get(key)
        if cand and Version(cand) > Version(cur_version):
            name = key.split(":", 1)[0]
            upgradable.append({"paket": name, "mevcut": cur_version, "aday": cand})
            upgradable_keys.append(key)

    cache_mtime = None
    if APT_LISTS_DIR.is_dir():
        try:
            cache_mtime = max(
                (p.stat().st_mtime for p in APT_LISTS_DIR.iterdir()), default=None
            )
        except OSError:
            cache_mtime = None

    return {
        "yukseltilebilir_paket_sayisi": len(upgradable),
        "yukseltilebilir_paketler": upgradable,
        "guvenlik_guncelleme_durumu": _security_update_age_days(upgradable_keys, candidate_src),
        "cache_son_guncelleme": (
            datetime.fromtimestamp(cache_mtime, tz=timezone.utc).isoformat()
            if cache_mtime else None
        ),
        "pro_esm_durumu": _pro_status(),
        "son_24s_otomatik_yuklenen": _unattended_upgrades_recent(),
    }


@mcp.tool()
def servis_saglik(servisler: list[str] | None = None) -> dict:
    """Failed systemd servislerini ve (verilirse) belirtilen servis adlarının
    aktiflik durumunu döner. servisler örn. ["nginx", "postgresql", "docker"]."""
    failed_raw = _run(["systemctl", "--failed", "--no-legend", "--plain"])
    failed = []
    if "hata" not in failed_raw:
        for line in failed_raw["stdout"].splitlines():
            parts = line.split(maxsplit=4)
            if len(parts) >= 4:
                failed.append(
                    {
                        "unit": parts[0],
                        "load": parts[1],
                        "active": parts[2],
                        "sub": parts[3],
                        "aciklama": parts[4] if len(parts) > 4 else "",
                    }
                )

    durumlar = {}
    for isim in servisler or []:
        r = _run(["systemctl", "is-active", isim])
        durumlar[isim] = r.get("stdout") or r.get("hata", "bilinmiyor")

    return {"failed_servisler": failed if "hata" not in failed_raw else failed_raw, "belirtilen_servisler": durumlar}


def _parse_syslog_ts(line: str) -> datetime | None:
    """'Jul 30 10:15:01 ...' formatındaki syslog zaman damgasını parse eder.
    Yıl bilgisi yok — mevcut yılı varsayar, gelecekte kalırsa bir önceki yıla çeker
    (yıl dönümünde eski kayıtları yanlış yorumlamamak için)."""
    now_year = datetime.now().year
    try:
        ts_str = " ".join(line.split()[:3])
        dt = datetime.strptime(f"{now_year} {ts_str}", "%Y %b %d %H:%M:%S")
    except (ValueError, IndexError):
        return None
    if dt.timestamp() > time.time():
        dt = dt.replace(year=now_year - 1)
    return dt


def _ssh_failed_last_24h() -> dict:
    cutoff = time.time() - 24 * 3600
    text = _read_text(AUTH_LOG)
    if text is not None:
        count = 0
        for line in text.splitlines():
            if "Failed password" not in line:
                continue
            dt = _parse_syslog_ts(line)
            if dt and dt.timestamp() >= cutoff:
                count += 1
        return {"sayi": count, "kaynak": "auth.log"}

    r = _run(
        ["journalctl", "-t", "sshd", "--since", "24 hours ago", "--no-pager", "-o", "cat"]
    )
    if "hata" in r:
        return {"hata": "auth.log yok ve journalctl de kullanılamadı: " + r["hata"]}
    count = sum(1 for line in r["stdout"].splitlines() if "Failed password" in line)
    return {"sayi": count, "kaynak": "journalctl (auth.log mount edilmemiş)"}


def _ssh_last_logins(limit: int = 20) -> dict:
    """Son başarılı SSH girişlerini auth.log'daki 'Accepted ...' satırlarından
    okur. `last`/wtmp'e bağımlı değildir — bazı dağıtımlarda `last` artık
    wtmpdb gibi ayrı bir pakete taşındığı için bilinçli olarak bu yol seçildi."""
    text = _read_text(AUTH_LOG)
    if text is not None:
        girisler = [line.strip() for line in text.splitlines() if "Accepted " in line and "sshd" in line]
        return {"girisler": girisler[-limit:], "kaynak": "auth.log"}

    r = _run(
        ["journalctl", "-t", "sshd", "--since", "-30 days", "--no-pager", "-o", "cat"]
    )
    if "hata" in r:
        return {"hata": "auth.log yok ve journalctl de kullanılamadı: " + r["hata"]}
    girisler = [line.strip() for line in r["stdout"].splitlines() if "Accepted " in line]
    return {"girisler": girisler[-limit:], "kaynak": "journalctl (auth.log mount edilmemiş, son 30 gün)"}


@mcp.tool()
def guvenlik_ozeti() -> dict:
    """Son 24 saatte başarısız SSH girişleri, son 20 başarılı SSH girişi ve
    aktif oturumlar."""
    aktif_oturumlar = _run(["who", str(UTMP)]) if UTMP.exists() else {
        "hata": "/run/utmp mount edilmemiş"
    }
    return {
        "ssh_basarisiz_giris_24s": _ssh_failed_last_24h(),
        "ssh_son_20_basarili_giris": _ssh_last_logins(),
        "aktif_oturumlar": aktif_oturumlar.get("stdout") or aktif_oturumlar.get("hata"),
    }


@mcp.tool()
def log_ozeti() -> dict:
    """Kernel (dmesg yerine journalctl -k) ve genel syslog uyarı/hata satırları,
    son 24 saat, maksimum 100 satır."""
    kernel = _run(
        [
            "journalctl", "-k", "-p", "warning..err",
            "--since", "24 hours ago", "-n", "100", "--no-pager",
        ]
    )
    syslog = _run(
        [
            "journalctl", "-p", "warning..err",
            "--since", "24 hours ago", "-n", "100", "--no-pager",
        ]
    )
    return {
        "kernel_uyari_hata": kernel.get("stdout") or kernel.get("hata"),
        "syslog_uyari_hata": syslog.get("stdout") or syslog.get("hata"),
    }


if __name__ == "__main__":
    _transport = os.environ.get("TRANSPORT", "stdio")
    _host = os.environ.get("MCP_HOST", "0.0.0.0")
    _port = int(os.environ.get("MCP_PORT", "8000"))
    if _transport == "stdio":
        mcp.run(transport="stdio")
    elif _transport == "streamable-http":
        mcp.run(transport="streamable-http", host=_host, port=_port)
    elif _transport == "sse":
        mcp.run(transport="sse", host=_host, port=_port)
    else:
        raise ValueError(f"Bilinmeyen TRANSPORT: {_transport!r}")
