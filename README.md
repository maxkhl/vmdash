# vmdash

Web-Dashboard für die Kunden-VMs auf dem Host **maxwork** (libvirt/KVM, Q35/UEFI,
Gäste mit LUKS-Vollverschlüsselung, Debian 13).

Bei jedem Befehl steht, wo er auszuführen ist:
**[Host]** = maxwork, **[VM]** = im Gast, **[Dev]** = Entwicklungsrechner.

## 1. Was die App tut

- **Starten mit Entsperren:** Nach einem Klick auf „Starten“ fragt das Dashboard die
  LUKS-Passphrase ab. Es startet die VM, tippt die Passphrase über die serielle
  Konsole am Boot-Prompt ein und meldet, ob das Entsperren geklappt hat. War die
  Passphrase falsch, fragt es erneut, ohne die VM neu zu starten.
- **Herunterfahren** (ACPI) und **Hart ausschalten** (`destroy`, nur nach Bestätigung).
- **Klonen:** Legt aus einer ausgeschalteten VM (Standard: `sap-template`) eine neue
  Kunden-VM an. Dazu gehören ein eigener LUKS-Keyslot, Hostname, machine-id und
  SSH-Host-Keys (siehe [Klonen](#8-klonen)). Das Dashboard ersetzt das Skript `vm-klon`.
- **RDP über rdpgw:** Bei einer laufenden VM mit bekannter IP führt „Verbinden“
  zum RD Gateway (rdpgw), das eine `.rdp`-Datei liefert. Ein einmal eingerichteter
  Starter öffnet sie mit FreeRDP, ohne VPN (siehe [Abschnitt 9](#9-rdp-über-rdpgw)).
  Ob RDP im Gast erreichbar ist, prüft vmdash nicht.

> **Keine eigene Anmeldung.** vmdash hat kein Login. Es muss hinter Authentik
> Forward Auth im Nginx Proxy Manager laufen (siehe [Abschnitt 5](#5-hinter-nginx-proxy-manager-mit-authentik)),
> genau wie sapfav. Den Port nie ungeschützt ins LAN freigeben.

Passphrasen werden nie gespeichert oder geloggt. Auch die Konsolenausgabe wird
nicht gespeichert, denn cryptsetup echot die Passphrase als Sternchen, und die
verraten ihre Länge.

## 2. Voraussetzungen auf dem Host

- libvirt mit `qemu:///system` und ein Storage-Pool (Standard `default`,
  `/var/lib/libvirt/images`), in dem die VM-Disks liegen.
- Docker (bzw. Dockge).
- GID der Gruppe des libvirt-Sockets ermitteln:

  ```sh
  # [Host]
  stat -c %g /var/run/libvirt/libvirt-sock
  ```

- Eine UID/GID, die **auf dem Host existiert**, für den Container (Standard 1000).
  libvirtd schlägt die UID des Aufrufers in der `/etc/passwd` des Hosts nach und
  verweigert sonst die Verbindung („Failed to find user record for uid …“):

  ```sh
  # [Host]
  id -u; id -g
  ```

## 3. Voraussetzungen im Gast bzw. im Template

Das Template `sap-template` braucht das einmalig, danach erben es alle Klone.

1. **Serielle Konsole für den LUKS-Prompt.** In `/etc/default/grub` muss `ttyS0`
   als letzte Konsole stehen, denn der Passwort-Prompt erscheint nur auf der
   letzten:

   ```sh
   # [VM]
   sudoedit /etc/default/grub
   #   GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8"
   sudo update-grub
   ```

2. **Guest-Agent installieren:**

   ```sh
   # [VM]
   sudo apt install qemu-guest-agent
   ```

3. **Agent-Kanal in der Domain-XML.** VMs aus virt-manager haben ihn meistens schon:

   ```sh
   # [Host]
   virt-xml sap-template --add-device --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0
   ```

4. **Serielles Gerät** in der Domain-XML (`<serial type='pty'>` plus `<console>`).
   Bei VMs aus virt-manager ist das Standard.

5. **Boot-Prompt testen.** Der Mitschnitt muss `Please unlock disk …:` zeigen:

   ```sh
   # [Host]
   script -q -c "virsh start sap-template --console" ~/boot-log.txt
   ```

6. **Agent testen.** In der Liste müssen `guest-exec`, `guest-exec-status`,
   `guest-file-open`, `guest-file-write` und `guest-file-close` mit
   `"enabled":true` stehen:

   ```sh
   # [Host], VM läuft und ist entsperrt
   virsh qemu-agent-command sap-template '{"execute":"guest-info"}' \
     | tr '{' '\n' | grep -E '"guest-(exec|exec-status|file-open|file-write|file-close)"'
   ```

## 4. Installation per Docker Compose / Dockge

### Variante A: selbst bauen (Standard in `docker-compose.yml`)

```sh
# [Host]
git clone https://github.com/maxkhl/vmdash.git
cd vmdash
cp .env.example .env
stat -c %g /var/run/libvirt/libvirt-sock     # GID notieren
nano .env                                     # LIBVIRT_SOCK_GID=<GID>, ggf. VMDASH_UID/VMDASH_GID
docker compose up -d --build
docker compose logs -f                        # muss "Verbunden mit qemu:///system" zeigen
```

In Dockge: neuen Stack anlegen, den Inhalt von `docker-compose.yml` einfügen, im
Feld `.env` den Inhalt von `.env.example` mit ausgefüllter `LIBVIRT_SOCK_GID`
eintragen. Für `build: .` muss der Stack-Ordner das geklonte Repository sein.
Sonst Variante B nehmen.

### Variante B: fertiges Image aus GHCR

Die CI baut bei jedem Push auf `main` die Images `ghcr.io/maxkhl/vmdash:latest` und
`ghcr.io/maxkhl/vmdash:<short-sha>` (amd64 und arm64).

In `docker-compose.yml` die Zeile `build: .` auskommentieren und
`image: ghcr.io/maxkhl/vmdash:latest` aktivieren. Ist das Paket privat, einmalig
anmelden, mit einem GitHub-Token mit Recht `read:packages`:

```sh
# [Host]
docker login ghcr.io -u maxkhl
docker compose pull && docker compose up -d
```

### Was die Compose-Datei macht

- Sie bindet den Host-Socket `/var/run/libvirt/libvirt-sock` in den Container ein.
  Der Container läuft als unprivilegierter Nutzer (`VMDASH_UID`/`VMDASH_GID`, muss
  auf dem Host existieren) und bekommt per `group_add` die Gruppe des Sockets.
- Sie veröffentlicht den Port standardmäßig nur auf `127.0.0.1:8000`
  (`VMDASH_PUBLISH`).
- Ein `HEALTHCHECK` ruft `/healthz` auf.

Hinweis: Legt libvirtd den Socket bei einem Neustart neu an (ohne
systemd-Socket-Aktivierung), zeigt der Bind-Mount ins Leere. Dann
`docker compose restart` ausführen.

## 5. Hinter Nginx Proxy Manager mit Authentik

Das Vorgehen ist wie bei sapfav:

1. Authentik: einen Proxy-Provider im Modus **Forward auth (single application)**
   für die externe URL (z. B. `https://vmdash.example.de`) und eine Application
   dazu anlegen, dann beide dem Outpost zuweisen.
2. NPM: einen Proxy-Host für diese Domain mit SSL anlegen. Als Ziel eine der
   beiden Varianten:
   - **Gemeinsames Docker-Netz (empfohlen, wenn NPM auf maxwork läuft):**
     vmdash ins Netz des NPM hängen (`networks:` in der Compose-Datei), den
     `ports:`-Eintrag entfernen und als Ziel `http://vmdash:8000` eintragen.
   - **Host-Port:** `VMDASH_PUBLISH=<Host-IP>:8000` setzen und den Port per
     Firewall nur für den NPM freigeben.
3. Unter *Advanced* die Forward-Auth-Konfiguration aus der Authentik-Doku
   eintragen (Abschnitt „Nginx Proxy Manager“ beim Proxy-Provider). Kern:
   `auth_request /outpost.goauthentik.io/auth/nginx;` im `location /` und ein
   `location /outpost.goauthentik.io`, das auf den Authentik-Outpost zeigt.

Die gesamte App einschließlich `/api/…` muss hinter der Authentifizierung
liegen. Schreibende API-Aufrufe verlangen `Content-Type: application/json`, damit
fremde Seiten keine Formulare an vmdash schicken können. HTTPS kommt vom NPM.

## 6. Konfiguration

Alle Einstellungen kommen aus Env-Variablen (in `.env`).

| Variable                | Default                                     | Bedeutung                                   |
| ----------------------- | ------------------------------------------- | ------------------------------------------- |
| `VMDASH_PORT`           | `8000`                                      | Port (im Container immer 8000)              |
| `VMDASH_BACKEND`        | `auto`                                      | `auto` / `libvirt` / `mock`                 |
| `VMDASH_LIBVIRT_URI`    | `qemu:///system`                            | libvirt-Verbindung                          |
| `VMDASH_PROMPT_REGEX`   | `Please unlock disk (\S+):`                 | Erkennung des LUKS-Prompts                  |
| `VMDASH_SUCCESS_REGEX`  | `cryptsetup: (\S+): set up successfully`    | Erkennung: erfolgreich entsperrt            |
| `VMDASH_FAIL_REGEX`     | `cryptsetup: ERROR: \S+: cryptsetup failed` | Erkennung: falsche Passphrase               |
| `VMDASH_PROMPT_TIMEOUT` | `120`                                       | Sekunden bis zum Prompt                     |
| `VMDASH_UNLOCK_TIMEOUT` | `30`                                        | Sekunden bis Erfolg/Fehler nach der Eingabe |
| `VMDASH_TEMPLATE_VM`    | `sap-template`                              | vorausgewählte Quelle beim Klonen           |
| `VMDASH_CLONE_PREFIX`   | `kunde-`                                    | Präfix für neue VM-Namen                    |
| `VMDASH_STORAGE_POOL`   | `default`                                   | libvirt-Storage-Pool                        |
| `VMDASH_VNC_PORT_MIN`   | `5910`                                      | VNC-Portbereich für Klone                   |
| `VMDASH_VNC_PORT_MAX`   | `5919`                                      | VNC-Portbereich für Klone                   |
| `VMDASH_LUKS_DEVICE`    | `/dev/vda3`                                 | LUKS-Gerät im Gast                          |
| `VMDASH_AGENT_TIMEOUT`  | `180`                                       | Sekunden, bis der Guest-Agent antworten muss |
| `VMDASH_LIBVIRT_NETWORK`| `default`                                   | libvirt-Netz der VMs (IPs, DHCP-Reservierungen) |
| `VMDASH_RDPGW_URL`      | leer                                        | RD Gateway für „Verbinden“, leer = aus      |
| `VMDASH_RDP_PORT`       | `3389`                                      | RDP-Port in den VMs                         |
| `VMDASH_LOG_LEVEL`      | `INFO`                                      | Log-Level                                   |
| `VMDASH_MOCK_DELAY`     | `1.0`                                       | nur Mock: Grundverzögerung in Sekunden      |
| `VMDASH_MOCK_FAIL_STEP` | leer                                        | nur Mock: Klon-Schritt, der fehlschlägt     |

Für die Compose-Datei gibt es außerdem `LIBVIRT_SOCK_GID` (Pflicht),
`VMDASH_UID`/`VMDASH_GID` (Standard 1000, muss auf dem Host existieren) und
`VMDASH_PUBLISH` (Standard `127.0.0.1:8000`).

Im Betrieb `VMDASH_BACKEND=libvirt` setzen (so steht es in `.env.example`). Mit
`auto` fällt vmdash bei einem Verbindungsproblem in den Mock-Modus zurück. Die
Oberfläche zeigt das zwar mit einem roten Banner „MOCK-MODUS“, aber nur der
Wert `libvirt` macht das Problem sofort als Fehler sichtbar.

## 7. Sicherheitshinweise

- **Der libvirt-Socket gibt dem Container faktisch Root-Rechte auf dem Host.** Wer
  vmdash bedienen kann, kann über libvirt beliebige VMs und Disks definieren.
  Deshalb muss Authentik davor, und der Port darf nicht offen im LAN hängen.
- **Über den Guest-Agent kann der Host Befehle als root in laufenden VMs
  ausführen.** vmdash nutzt das beim Klonen. Das gilt für jede VM mit
  Agent-Kanal, nicht nur für vmdash.
- Passphrasen stehen nur im Request-Body und im Speicher des laufenden Jobs. Sie
  landen nie in Logs, API-Antworten oder Fehlermeldungen.
- Im Gast liegen sie nur kurz als Datei unter `/run` (tmpfs, Rechte 600) und
  werden sofort gelöscht. Als Kommandozeilenargument werden sie nie übergeben.
- Beim Klonen gehen Passphrasen als (base64-kodierter) Inhalt von
  `guest-file-write` durch libvirt. Ist in libvirt Debug-Logging aktiv
  (`log_filters` mit Level 1 für `qemu`), können sie dort auftauchen. Debug-Logging
  daher nur zur Fehlersuche und nicht während eines Klons einschalten.

## 8. Klonen

Den Dialog öffnet „Neue VM klonen“ oder „Klonen“ auf einer ausgeschalteten VM.
Er fragt ab:

- das Kundenkürzel (der neue Name wird `kunde-<kürzel>`),
- die aktuelle LUKS-Passphrase der Quelle,
- die neue Passphrase (zweimal).

### Automatische Prüfungen vor dem Klonen

vmdash prüft vor jeder Aktion:

- Die Quelle ist ausgeschaltet.
- Name und Volume `<name>.qcow2` sind frei.
- Im Pool ist mindestens das 1,2-fache der belegten Größe der Quell-Disk frei
  (maxwork hat nur eine 256-GB-NVMe).
- Im Bereich 5910–5919 ist ein VNC-Port frei.
- Die Quelle hat einen Guest-Agent-Kanal und eine Netzwerkkarte im libvirt-Netz
  (`VMDASH_LIBVIRT_NETWORK`).
- Im Netz gibt es noch keine DHCP-Reservierung mit dem neuen Namen, und im
  DHCP-Bereich ist eine IP frei.

### Ablauf

Jeder Schritt erscheint mit Status in der VM-Karte.

1. **Disk klonen** über die libvirt-Storage-API (entspricht `virsh vol-clone`).
   Die internen qcow2-Snapshots des Templates werden dabei eingeflacht.
   `virt-clone` würde an diesen Snapshots scheitern.
2. **Domain-XML anpassen und definieren.** Dabei ändern sich:
   - der Name,
   - die UUID und die MAC-Adressen (vergibt libvirt neu),
   - der Disk-Pfad,
   - der VNC-Port (fest, `autoport='no'`).
   - NVRAM: Die VARS-Datei der Quelle wird zum `template`, der Klon bekommt
     `<name>_VARS.fd`. So bleiben die UEFI-Boot-Einträge erhalten.
3. **Feste IP reservieren:** Vor dem ersten Start wählt vmdash die niedrigste
   freie IP im DHCP-Bereich (nicht reserviert, nicht verliehen) und trägt sie mit
   der neuen MAC und dem VM-Namen als `<host>` ins Netz ein (live und dauerhaft,
   entspricht `virsh net-update … add-last ip-dhcp-host`).
4. **Klon starten** und mit der **alten** Passphrase entsperren.
5. **Auf den Guest-Agent warten.**
6. **Neue Passphrase als zusätzlichen Keyslot** hinzufügen
   (`cryptsetup luksAddKey`, Schlüssel als Dateien in `/run`).
7. **Hostname** setzen (auch in `/etc/hosts`).
8. **xrdp-Zertifikat neu erzeugen.** Sonst erbt der Klon das Zertifikat des
   Templates (CN `sap-template`). Das geht nur, wenn `/etc/xrdp/cert.pem` und
   `key.pem` Symlinks auf das Snakeoil-Zertifikat sind (Debian-Standard). Dann
   laufen `make-ssl-cert generate-default-snakeoil --force-overwrite` und
   `systemctl restart xrdp`. Andernfalls bricht der Schritt mit einer Meldung ab.
   Ohne `/etc/xrdp` wird er übersprungen.
9. **machine-id** und **SSH-Host-Keys** neu erzeugen.
10. **Neustart** und Entsperren mit der **neuen** Passphrase. Klappt das nicht,
    bricht der Ablauf ab. Der alte Keyslot ist dann noch aktiv, und der Klon lässt
    sich weiter mit der Passphrase der Quelle öffnen.
11. Erst jetzt wird der **alte Keyslot entfernt** (`cryptsetup luksRemoveKey`).

Bei einem Fehler bleibt der Klon bestehen. „Klon löschen“ entfernt dann nach
Bestätigung Domain, Volume, NVRAM-Datei und die DHCP-Reservierung.

### Manuelle Checkliste nach dem Klonen

Die Liste erscheint auch in der Oberfläche:

- [ ] Neue Passphrase im Passwortmanager ablegen.
- [ ] In Authentik einen RAC-Endpunkt für den neuen VNC-Port (`127.0.0.1:<port>`) anlegen.
- [ ] Kunden-VPN im Gast installieren.
- [ ] SAP-GUI-Verbindung und ABAP-Projekt in Eclipse einrichten.

Für RDP ist nichts zu tun: Der Klon hat eine feste IP, und rdpgw erlaubt jedes
Ziel im VM-Netz (Entscheidung A, siehe [Abschnitt 9](#9-rdp-über-rdpgw)).

## 9. RDP über rdpgw

Die VMs hängen im libvirt-NAT-Netz und sind aus dem LAN nicht direkt erreichbar.
RDP läuft deshalb über [rdpgw](https://github.com/bolkedebruin/rdpgw), ein
RD Gateway mit Anmeldung über Authentik:

1. **[Browser]** „Verbinden“ ist ein normaler Link auf
   `VMDASH_RDPGW_URL/connect?host=<VM-IP>:3389`. Die `.rdp`-Datei muss direkt im
   Browser von rdpgw kommen, weil das Token darin an die Browser-Sitzung und die
   Client-IP gebunden ist.
2. **[Browser]** rdpgw meldet über Authentik an und liefert die `.rdp`-Datei.
3. **[Client]** Der eingerichtete Starter öffnet sie mit FreeRDP über das Gateway
   (Websocket-Transport).

Die IP einer VM kommt aus ihrer DHCP-Reservierung im Netz (über die MAC aus der
Domain-XML), ersatzweise aus der aktuellen Lease. Ohne IP zeigt die VM-Karte
einen Hinweis statt des Knopfs.

### Client einrichten

In der Kopfzeile führt „RDP-Client einrichten“ zu einer kurzen Anleitung.

- **Linux:** Das Skript `client/vmdash-rdp-setup.sh` herunterladen und einmal
  ausführen. Es installiert FreeRDP als Flatpak (`com.freerdp.FreeRDP`) für den
  aktuellen Benutzer und meldet einen Starter für `.rdp`-Dateien an. Danach im
  Browser einstellen, dass `.rdp`-Dateien immer geöffnet werden.

  ```sh
  # [Client]
  bash vmdash-rdp-setup.sh               # einrichten
  bash vmdash-rdp-setup.sh --uninstall   # entfernen
  ```

- **Windows:** Keine Einrichtung nötig, der eingebaute Client öffnet `.rdp`-Dateien
  direkt. Noch nicht getestet (siehe [Offen](#12-offen)).

Remmina ist nicht geeignet: Es schaltet den Websocket-Transport ab und übernimmt
die Einstellung nicht aus `.rdp`-Dateien. rdpgw lehnt den klassischen
HTTP-Transport ab.

### rdpgw aufsetzen

Die Vorlagen liegen in [`docs/rdpgw/`](docs/rdpgw/):

- `compose.yaml.example` für den Dockge-Stack auf maxwork: im Host-Netz, Port 8443,
  Entrypoint direkt auf `/opt/rdpgw/rdpgw -c …`.
- `rdpgw.yaml.example` mit Platzhaltern statt Secrets.
- `npm-advanced.conf` für den Proxy-Host im NPM.

**Authentik (OIDC):**

1. Einen OAuth2/OpenID-Provider anlegen: Typ „Confidential“, Redirect-URI
   `https://rdpgw.maxkhl.com/callback`.
2. Eine Application mit Slug `rdpgw` anlegen und mit dem Provider verbinden.
3. Unter „Policy / Group / User Bindings“ nur den eigenen Benutzer binden.
4. Client-ID und Client-Secret in `rdpgw.yaml` eintragen; `ProviderUrl` ist
   `https://<authentik-host>/application/o/rdpgw/`.

**Nginx Proxy Manager (auf maxmedia):**

- Proxy-Host `rdpgw.maxkhl.com` auf `http://<maxwork-ip>:8443` anlegen.
- „Websockets Support“ einschalten.
- Im SSL-Tab **HTTP/2 ausschalten**.
- Unter *Advanced* den Inhalt von `npm-advanced.conf` eintragen: `proxy_buffering off`,
  `proxy_request_buffering off`, `chunked_transfer_encoding off` und lange Timeouts.

**Stack starten:**

```sh
# [Host maxwork]
sudo mkdir -p /opt/stacks/rdpgw
sudo cp docs/rdpgw/compose.yaml.example /opt/stacks/rdpgw/compose.yaml
sudo cp docs/rdpgw/rdpgw.yaml.example /opt/stacks/rdpgw/rdpgw.yaml    # dann ausfüllen
sudo chown 1001:1001 /opt/stacks/rdpgw/rdpgw.yaml
sudo chmod 600 /opt/stacks/rdpgw/rdpgw.yaml
cd /opt/stacks/rdpgw && sudo docker compose up -d
```

**Welche Ziele rdpgw erlaubt (Entscheidung A):** `HostSelection: any` mit
`AllowPrivateDestinations: true` und `AllowedDestinationPorts: [3389]`. Neue Klone
funktionieren damit sofort, vmdash muss rdpgw nicht anfassen. Abgesichert wird
über zwei Wege:

- In Authentik ist nur der eigene Benutzer an die Application gebunden.
- Die Firewall auf maxwork (siehe unten) lässt Port 3389 nur im VM-Netz zu.

**Firewall auf maxwork:**

- Port 8443 ist nur von maxmedia erreichbar, für IPv4 und IPv6.
- rdpgw (UID 1001, Host-Netz) darf Port 3389 nur im VM-Netz ansprechen.

Die Regeln sind nicht persistent (siehe [Offen](#12-offen)):

```sh
# [Host maxwork] eingehend: 8443 nur von maxmedia
sudo iptables  -I INPUT -p tcp --dport 8443 -j DROP
sudo iptables  -I INPUT -p tcp --dport 8443 -s <maxmedia-ipv4> -j ACCEPT
sudo ip6tables -I INPUT -p tcp --dport 8443 -j DROP
sudo ip6tables -I INPUT -p tcp --dport 8443 -s <maxmedia-ipv6> -j ACCEPT

# [Host maxwork] ausgehend: rdpgw darf 3389 nur im VM-Netz ansprechen
sudo iptables -I OUTPUT -m owner --uid-owner 1001 -p tcp --dport 3389 ! -d 192.168.122.0/24 -j REJECT
```

Weitere Ports blockiert rdpgw selbst über `AllowedDestinationPorts: [3389]`.

**DNS:** maxwork muss `*.maxkhl.com` über AdGuard auflösen, also auf die interne
Adresse von maxmedia. Sonst erreicht rdpgw Authentik nicht (OIDC-Discovery und
Token-Prüfung).

```sh
# [Host maxwork] Prüfen
resolvectl query <authentik-host>
```

**vmdash einschalten:** In `.env` von vmdash `VMDASH_RDPGW_URL=https://rdpgw.maxkhl.com`
setzen und den Stack neu starten.

## 10. Entwicklung

Entwickelt wird ohne Zugriff auf die VMs. Das Mock-Backend simuliert drei VMs,
den Boot mit dem echten Konsolenmitschnitt (`testdata/boot-debian13-luks.txt`),
den Guest-Agent und alle Klon-Schritte. Die Passphrase `falsch` wird abgelehnt,
jede andere angenommen.

```sh
# [Dev]
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
VMDASH_BACKEND=mock .venv/bin/python -m app       # http://localhost:8000
.venv/bin/python -m pytest -q
# shellcheck über das Einrichtungsskript und den darin eingebetteten Starter
.venv/bin/shellcheck client/vmdash-rdp-setup.sh
awk "/<< 'EOF'/{f=1;next} /^EOF\$/{f=0} f" client/vmdash-rdp-setup.sh > /tmp/vmdash-rdp-open
.venv/bin/shellcheck /tmp/vmdash-rdp-open
```

Nützliche Varianten:

```sh
# [Dev] schneller Mock bzw. absichtlicher Fehler in einem Klon-Schritt
VMDASH_BACKEND=mock VMDASH_MOCK_DELAY=0.2 .venv/bin/python -m app
VMDASH_BACKEND=mock VMDASH_MOCK_FAIL_STEP=unlock_new .venv/bin/python -m app
```

Mit einem entfernten Host (ohne Container, `libvirt-python` nötig, z. B. per
`sudo dnf install python3-libvirt` bzw. `apt install python3-libvirt` und einer
venv mit `--system-site-packages`):

```sh
# [Dev]
VMDASH_BACKEND=libvirt VMDASH_LIBVIRT_URI=qemu+ssh://maxkhl@maxwork/system .venv/bin/python -m app
```

Aufbau:

| Pfad                              | Inhalt                                               |
| --------------------------------- | ---------------------------------------------------- |
| `app/backends/base.py`            | Interface `VmBackend`                                 |
| `app/backends/libvirt_backend.py` | echte Implementierung (libvirt-python)                |
| `app/backends/mock_backend.py`    | Simulation                                            |
| `app/unlock.py`                   | Entsperren über die serielle Konsole                  |
| `app/clone.py`                    | Klon-Ablauf inkl. XML-Anpassung                       |
| `app/jobs.py`                     | Job-Verwaltung (nur im Speicher)                      |
| `app/web.py`                      | Routen und API                                        |
| `app/network.py`                  | IP-Ermittlung und IP-Auswahl im libvirt-Netz          |
| `app/static/`                     | Frontend (Vanilla-JS, ohne Build-Schritt)             |
| `client/vmdash-rdp-setup.sh`      | Einrichtung des RDP-Starters (Linux-Client)           |
| `docs/rdpgw/`                     | Vorlagen für rdpgw, NPM                               |
| `testdata/`                       | echter Boot-Mitschnitt und echte Template-XML         |

### API

| Methode | Pfad                       | Body / Antwort                                            |
| ------- | -------------------------- | --------------------------------------------------------- |
| GET     | `/healthz`                 | –                                                         |
| GET     | `/api/info`                | Backend-Modus, Version, Template-Name                     |
| GET     | `/api/vms`                 | VMs mit Zustand, Job, IP und rdpgw-Link                   |
| POST    | `/api/vms/{name}/start`    | `{passphrase}`                                            |
| POST    | `/api/vms/{name}/unlock`   | `{passphrase}`, erneute Eingabe nach `wrong_key`          |
| POST    | `/api/vms/{name}/shutdown` | –                                                         |
| POST    | `/api/vms/{name}/destroy`  | –                                                         |
| POST    | `/api/vms/{name}/clone`    | `{suffix, old_passphrase, new_passphrase}`                |
| POST    | `/api/vms/{name}/delete`   | –, nur für fehlgeschlagene Klone                          |
| GET     | `/rdp-client`              | Anleitung zur Client-Einrichtung                          |
| GET     | `/client/vmdash-rdp-setup.sh` | Einrichtungsskript für Linux                           |
| GET     | `/api/jobs/{id}`           | Job-Status mit Schritt-Liste                              |

## 11. Manueller Test auf maxwork

Diese Punkte sind nur gegen den echten Host prüfbar, die automatischen Tests
laufen gegen das Mock-Backend.

1. **[Host]** Nach `docker compose up -d` zeigt `docker compose logs` die Zeile
   „Verbunden mit qemu:///system“ und keine Mock-Warnung.
   **[Browser]** Es erscheint kein rotes Banner.
2. **[Browser]** Die VM-Liste stimmt mit `virsh list --all` **[Host]** überein.
3. **[Browser]** Eine ausgeschaltete Kunden-VM starten, erst mit falscher, dann
   mit richtiger Passphrase. Erwartet: „Passphrase falsch“, der Dialog kommt
   erneut, danach „Entsperrt“. **[Host]** `virsh domstate <vm>` zeigt `running`.
4. **[Browser]** „Herunterfahren“: Die VM geht aus (ACPI).
   „Hart ausschalten“ auf einer laufenden Test-VM: erst Bestätigung, dann sofort aus.
5. **[Browser]** Mit gesetzter `VMDASH_RDPGW_URL` zeigt eine laufende VM ihre IP
   und „Verbinden“. **[Client]** Nach `bash vmdash-rdp-setup.sh` öffnet ein Klick
   FreeRDP und verbindet über rdpgw. Eine laufende VM ohne IP zeigt den Hinweis.
6. **[Browser]** Klon von `sap-template` mit Kürzel `test` anlegen. Alle Schritte
   müssen grün werden. Dann prüfen:
   - **[Host]** `virsh dumpxml kunde-test | grep -E "nvram|vnc|mac|source file"`:
     NVRAM mit `template=…sap-template_VARS.fd` und Pfad `kunde-test_VARS.fd`,
     VNC-Port aus dem Bereich, neue MAC-Adresse.
   - **[Host]** `ls -l /var/lib/libvirt/qemu/nvram/kunde-test_VARS.fd` muss
     existieren (libvirt legt die Datei beim ersten Start an).
   - **[Host]** Den Boot in der VNC-Konsole beobachten: Er muss direkt
     `debian`/shim starten, nicht im UEFI-Menü landen. Damit ist die
     NVRAM-Übernahme geprüft.
   - **[Host]** `sudo qemu-img snapshot -l -U /var/lib/libvirt/images/kunde-test.qcow2`
     listet keine internen Snapshots.
   - **[VM]** `hostname` und `cat /etc/hosts` zeigen `kunde-test`.
   - **[VM]** `cat /etc/machine-id` unterscheidet sich vom Template.
   - **[VM]** `ls -l /etc/ssh/ssh_host_*` zeigt frische Zeitstempel.
   - **[VM]** `ls /run/vmdash-*` findet nichts.
   - **[Host]** `virsh net-dumpxml default | grep kunde-test` zeigt die Reservierung
     mit der MAC aus `virsh domiflist kunde-test`. Dieselbe Zeile steht auch in
     `virsh net-dumpxml default --inactive`.
   - **[VM]** `openssl x509 -noout -subject -in /etc/xrdp/cert.pem` zeigt
     `CN = kunde-test`.
   - **[Browser]** „Verbinden“ auf `kunde-test` öffnet eine RDP-Sitzung über rdpgw.
   - **[VM]** `sudo cryptsetup luksDump /dev/vda3` zeigt genauso viele Keyslots
     wie das Template.
7. **[Browser]** `kunde-test` herunterfahren und neu starten: Die neue Passphrase
   funktioniert, die des Templates wird abgelehnt.
8. **[Browser]** `sap-template` starten: Die bisherige Passphrase funktioniert
   weiter. Das Template ist unverändert.
9. **[Browser]** Fehlerpfad: einen Klon `test2` mit dreimal falscher alter
   Passphrase anlegen. Erwartet: „fehlgeschlagen“ und der Knopf „Klon löschen“.
   Nach dem Löschen zeigen **[Host]** `virsh list --all`,
   `virsh vol-list default`, `ls /var/lib/libvirt/qemu/nvram/` und
   `virsh net-dumpxml default` keine Reste von `kunde-test2`.
10. **Konsole belegt.** **[Browser]** Eine VM mit falscher Passphrase starten und
    den Wiederholungs-Dialog offen lassen. **[Host]** In dieser Zeit
    `virsh console <vm>` aufrufen. Erwartet: virsh meldet
    „Active console session exists for this domain“. vmdash hält die Konsole
    und übernimmt umgekehrt nie eine fremde Sitzung.
11. **[Host]** Aufräumen:
    `virsh destroy kunde-test; virsh undefine kunde-test --nvram --remove-all-storage`.

## 12. Offen

- **Windows-Client:** `mstsc` verlangt laut rdpgw-README Hostnamen statt IPs in
  der Host-Angabe. Das wird erst bei Bedarf angegangen.
- **Firewall-Regeln** auf maxwork sind nicht persistent.
- **`PAATokenEncryptionKey`** ist in rdpgw noch nicht gesetzt.
