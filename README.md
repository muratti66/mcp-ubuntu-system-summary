# Ubuntu System Manager MCP Server

[English](README_EN.md)

Ubuntu sunucusunun genel durumunu (sürüm, kaynak kullanımı, apt güncellemeleri,
servis sağlığı, güvenlik özeti, loglar, donanım) AI istemcilerine (Claude
Desktop, Claude Code, Open WebUI) MCP tool'ları olarak sunan **tamamen
salt-okunur** bir server. Sistemde hiçbir değişiklik yapmaz.

## Tool'lar

| Tool | Kapsam |
|---|---|
| `sistem_ozeti` | Ubuntu sürümü, kernel, uptime, CPU, fiziksel/sanal |
| `kaynak_kullanimi` | Load average, RAM/swap, disk/inode kullanımı |
| `wan_ip_getir` | Dış IP adresi |
| `guncelleme_durumu` | Yükseltilebilir paketler, güvenlik güncellemesi durumu, ESM, unattended-upgrades geçmişi |
| `servis_saglik` | Failed systemd servisleri + belirtilen servislerin durumu |
| `guvenlik_ozeti` | SSH başarısız giriş (24s), son 20 başarılı SSH girişi, aktif oturumlar |
| `log_ozeti` | journalctl ile kernel + syslog uyarı/hata (24s, maks. 100 satır) |

## Mimari: neden bu şekilde

Bu server host'un durumunu **değiştirmeden**, mümkün olduğunca **root
gerektirmeden** okumak üzerine kurulu. Container'a hiçbir yazma izni ya da
geniş capability verilmiyor; her veri kaynağı için host'tan sadece ilgili
dosya/dizin salt-okunur (`:ro`) mount ediliyor.

- **Disk/inode kullanımı** — host'un tüm kök dosya sistemini mount etmek
  yerine (geniş bir yüzey açardı), her izlenecek partition'da **boş bir
  "probe" dizini** oluşturulup sadece o boş dizin mount ediliyor.
  `statvfs()` bir dizinin içeriğine değil üzerinde bulunduğu dosya sistemine
  bakar, bu yüzden boş bir dizin bile partition'ın gerçek disk kullanımını
  verir — container host'un gerçek dosyalarını hiç görmez.
- **Apt güncelleme durumu** — container kendi `apt-get update`'ini
  **çalıştırmaz** (hem root/network gerektirir hem her çağrıda yavaştır).
  Bunun yerine host'taki `apt-daily.timer` (ya da bir cron) cache'i
  periyodik tazeler; container `/var/lib/apt/lists` ve
  `/var/lib/dpkg/status`'u salt-okunur mount edip `python-debian` ile
  doğrudan parse eder. Cache'in yaşı (`cache_son_guncelleme`) response'a
  eklenir ki host'taki timer bozulursa sessizce bayat veri dönmesin.
- **Servis sağlığı** — `systemctl --failed` / `is-active` gibi read-only
  sorgular host'un D-Bus system bus soketine (`/run/dbus/system_bus_socket`)
  bağlanarak çalışır. systemd'nin polkit politikasında read-only unit
  sorguları genelde authentication istemez (sadece start/stop/restart
  ister) — root gerekmemesi beklenir, ama bu davranış dağıtımdan dağıtıma
  farklılık gösterebileceği için hedef sistemde doğrulanmalı.
- **Güvenlik özeti** — `ufw`/`iptables` açık port özeti bilinçli olarak
  **kapsam dışı** bırakıldı: `ufw status` non-root kullanıcıda doğrudan
  reddediyor ve `--cap-add=NET_ADMIN` gibi geniş bir capability gerektiriyor,
  bu da "minimal yetki" hedefiyle çelişiyor. Son başarılı girişler için de
  `last`/wtmp yerine `auth.log`'daki `Accepted ...` satırları parse
  ediliyor — bazı dağıtımlarda (ör. Debian trixie) `last` komutu artık
  klasik `util-linux`'ta değil, ayrı bir pakette (`wtmpdb`) geliyor; bu hem
  ekstra bağımlılık hem de host'un klasik wtmp formatı kullandığı
  varsayımına dayanıyordu.
- **Loglar** — `dmesg` yerine **`journalctl -k`** kullanılıyor. Ham
  `dmesg`/`/dev/kmsg` erişimi `kernel.dmesg_restrict` sysctl'i yüzünden
  dosya izniyle değil `CAP_SYSLOG` capability'siyle (ya da root'la)
  açılıyor; `journalctl` aynı bilgiyi sadece `systemd-journal` grup
  üyeliğiyle sunuyor — ek capability gerekmiyor.
- **Donanım (fiziksel/sanal)** — `systemd-detect-virt` container içinde
  çalıştırılırsa **host'u değil container'ın kendisini** algılar (muhtemelen
  "docker" döner). Bunun yerine `/sys/class/dmi/id/*` dosyaları doğrudan
  okunup bilinen VM imzalarıyla (KVM, VMware, VirtualBox, GCE, AWS...)
  karşılaştırılıyor.
- **Network** — local arayüz listesi/routing tablosu bilinçli olarak kapsam
  dışı bırakıldı (recon/bilgi ifşası riski, özellikle server WAN'a açıksa).

Sonuç: `docker run`'a hiçbir `--cap-add` gerekmiyor, container varsayılan
(dropped) capability setiyle çalışabiliyor; tek istisna, host D-Bus'ına ve
belirli log/config dosyalarına erişim için gereken salt-okunur mount'lar ve
grup üyelikleri.

## Kurulum

### 1. Host tarafı hazırlık

**Grup ID'lerini bul** (container'ın root olmadan log dosyalarını okuyabilmesi için):

```bash
getent group adm systemd-journal utmp
```

`.env.example`'ı `.env` olarak kopyala ve GID'leri güncelle:

```bash
cp .env.example .env
```

**Disk probe dizinleri oluştur** — izlemek istediğin her mount noktasında
boş bir dizin (yukarıdaki "Mimari" bölümüne bakın):

```bash
sudo mkdir -p /.mcp-disk-probe
# ek mount noktaları için, örn.:
# sudo mkdir -p /home/.mcp-disk-probe
# sudo mkdir -p /var/.mcp-disk-probe
```

Her ek probe dizini için `compose.yaml`'daki `volumes` listesine karşılık gelen
satırı ekle/aç.

**Apt cache'ini taze tut** — container kendi `apt-get update`'ini çalıştırmaz,
host'taki `apt-daily.timer`'ı saatlik çalışacak şekilde ayarla:

```bash
sudo systemctl edit apt-daily.timer
```

açılan dosyaya ekle:

```ini
[Timer]
OnCalendar=
OnCalendar=hourly
```

kaydet, sonra:

```bash
sudo systemctl restart apt-daily.timer
```

### 2. Build & çalıştır

```bash
docker compose build
docker compose up -d
```

`guncelleme_durumu` tool'unun cevabındaki `cache_son_guncelleme` alanı, apt
cache'inin ne kadar taze olduğunu gösterir — bu değer sürekli eskiyorsa
`apt-daily.timer`'ı kontrol et.

### 3. AI istemcisine bağla

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

**Open WebUI (streamable-http):** `.env`'de `TRANSPORT=streamable-http` yap,
`compose.yaml`'da `ports` bloğunu aç (tercihen sadece bir intranet/WireGuard
arayüzüne bind et), `docker compose up -d` sonrası `http://<host>:8000/mcp`
adresini Open WebUI'ye tanıt.

## Bilinen sınırlamalar / doğrulanması gerekenler

- **Ubuntu Pro/ESM durumu** (`guncelleme_durumu.pro_esm_durumu`) — `pro`
  komutunun root olmadan cache'lenmiş durumu mı döndüğü yoksa reddedip
  `kullanılamıyor` mu döneceği hedef sistemde test edilmemiştir.
- **Servis sağlığı** (`servis_saglik`) — read-only `systemctl` sorgularının
  container içinden host D-Bus'ına root gerektirmeden erişip erişemediği
  hedef sistemde test edilmemiştir; polkit politikası dağıtımdan dağıtıma
  farklılık gösterebilir.
- **`apt list --upgradable` karşılaştırması** apt'nin pinning/priority
  kurallarını uygulamaz — sadece görülen en yüksek versiyonu aday alır. Çok
  özel pinning kullanan sistemlerde apt'nin gerçek davranışından sapabilir.
- **Güvenlik güncelleme yaşı** (`cache_yasi_gun`), ilgili `-security`
  reposunun yerel cache'inin ne kadar eski olduğunu gösterir — belirli bir
  paketin ilk yayınlanma tarihi değildir.
- **22.04+ minimal kurulumlarda** rsyslog paketi olmayabilir ve
  `/var/log/auth.log` hiç oluşmayabilir — bu durumda SSH giriş verileri
  otomatik olarak `journalctl -t sshd` fallback'inden okunur.

## Güvenlik notları

- `auth.log`/`syslog` içeriği IP adresi, kullanıcı adı gibi hassas veri
  içerebilir. Bu server'ı WAN'a (ör. Open WebUI ile) açacaksan bu tool'ları
  kapsam dışı bırak ya da erişimi bir VPN/intranet katmanıyla sınırla.
- Container `read_only: true`, `cap_drop: [ALL]` ve `no-new-privileges` ile
  çalışır; tüm mount'lar `:ro`.

## Lisans

[MIT](LICENSE)
