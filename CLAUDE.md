# CLAUDE.md – vmdash

Web-Dashboard für LUKS-verschlüsselte KVM-Gäste auf maxwork: Starten und
Entsperren über die serielle Konsole, Herunterfahren, Klonen, RDP-Link.
UI, Doku und Fehlermeldungen sind auf Deutsch.

## Entscheidungen (nicht ohne Grund ändern)

- **Stack:** Python 3 + Flask + waitress, libvirt-python, Vanilla-JS/CSS ohne
  Build-Schritt und ohne CDN (muss offline im LAN laufen). Kein Go, kein PHP.
  Im Container kommen die Pakete per apt aus Debian trixie, nicht per pip.
- **Genau ein Prozess** (waitress mit Threads). Jobs liegen nur im Arbeitsspeicher
  (`app/jobs.py`); mehrere Worker würden sie nicht teilen.
- **Keine Datenbank.** Der VM-Zustand kommt immer live aus libvirt.
- **Keine eigene Anmeldung**; Authentik Forward Auth im NPM davor.
  Schreibende Requests verlangen `Content-Type: application/json` (CSRF-Schutz).
- **Backend-Abstraktion** `app/backends/base.py` mit `libvirt_backend.py` und
  `mock_backend.py`. `VMDASH_BACKEND=auto` fällt ohne libvirt laut geloggt auf
  den Mock zurück; die UI zeigt dann das Banner „MOCK-MODUS“. Tests laufen
  ausschließlich gegen den Mock bzw. einen Fake-Serial-Stream.
- **Alles über den libvirt-Socket:** Disk-Kopie per `storageVolCreateXMLFrom`
  (= `virsh vol-clone`), kein virt-clone, kein qemu-img, kein Dateizugriff auf
  Images. Grund: Das Template hat interne qcow2-Snapshots, virt-clone verweigert
  sie; vol-clone flacht sie ein.
- **Konsole:** VM angehalten starten (`VIR_DOMAIN_START_PAUSED`), Konsole öffnen,
  dann `resume`, damit kein Output vor dem Prompt verloren geht. `openConsole` mit
  Flags 0; eine belegte Konsole wird nie übernommen. Nicht-blockierende Streams
  brauchen die libvirt-Event-Loop (Thread in `libvirt_backend.py`).
- **Prompt-Erkennung** auf dem gepufferten Output, nicht zeilenweise: Der Prompt
  endet ohne Zeilenumbruch. Die Regexe stammen aus dem echten Mitschnitt
  `testdata/boot-debian13-luks.txt` (Debian 13, cryptsetup-initramfs). Nach einer
  falschen Passphrase gilt nur ein neuer Prompt als „wrong_key“; „maximum number
  of tries exceeded“ oder kein Prompt heißt „failed“.

## Container

Der Container muss mit einer UID laufen, die auf dem Host existiert
(`user:` in der Compose-Datei, `VMDASH_UID`). libvirtd schlägt die UID des
Aufrufers per SO_PEERCRED in der Host-`/etc/passwd` nach und lehnt unbekannte ab.

## Passphrasen

- Nur im Request-Body; nie in Logs, Job-Status, API-Antworten oder
  Fehlermeldungen. Konsolenausgabe nie speichern oder loggen, weil das
  Sternchen-Echo die Länge verrät.
- Start: nach dem Eintippen verwerfen. Klon: alte bis Schritt 11, neue bis
  Schritt 10, danach und bei jedem Abbruch verwerfen (`secrets.clear()` in `finally`).
- Im Gast nur als Datei unter `/run`. Zuerst mit `install -m 600 /dev/null` anlegen,
  dann per `guest-file-write` schreiben (sonst 0644), ohne abschließenden
  Zeilenumbruch, und sofort löschen, auch im Fehlerfall. Nie als argv übergeben.
- Frontend: nur `type=password`, Felder nach dem Senden leeren.

## Klonen: Reihenfolge der Keyslots

Erst die neue Passphrase hinzufügen (`luksAddKey`), dann neu starten und mit der
neuen entsperren. Nur wenn das klappt, den alten Keyslot entfernen
(`luksRemoveKey`). Scheitert das Entsperren mit der neuen Passphrase, wird
abgebrochen; der Hinweis „alter Keyslot noch aktiv“ gehört in die Meldung.
Nie `--batch-mode` bei `luksRemoveKey` (Schutz vor Entfernen des letzten Slots).

Weitere Punkte beim Klonen:

- Vorbedingungen werden synchron im Request geprüft (400 mit verständlicher
  Meldung), bevor irgendetwas angelegt wird.
- „Klon löschen“ ist nur für fehlgeschlagene Klone erlaubt, deren Volume dieser
  Job selbst angelegt hat.
- NVRAM der Quelle wird als `template` gesetzt, die neue Datei heißt
  `<name>_VARS.fd`.

## RDP über rdpgw

- „Verbinden“ ist ein normaler Link auf `VMDASH_RDPGW_URL/connect?host=<ip>:<port>`,
  kein JavaScript-Abruf. Die `.rdp`-Datei muss direkt im Browser von rdpgw kommen,
  weil das Token an Browser-Sitzung und Client-IP gebunden ist.
- Die IP kommt aus der DHCP-Reservierung im libvirt-Netz (Zuordnung über die MAC
  aus der Domain-XML), ersatzweise aus der aktuellen Lease (`app/network.py`).
  Direkte IPs aus dem NAT-Netz sind von außen nicht erreichbar; deshalb gibt es
  keinen `rdp://`-Link und kein `VMDASH_VM_CONFIG` mehr.
- Der Client MUSS den Websocket-Transport nutzen. Den klassischen HTTP-Transport
  (RDG_OUT_DATA + RDG_IN_DATA) lehnt rdpgw ab („rejecting reuse of
  Rdg-Connection-Id … from a different identity“).
- Remmina schaltet Websocket standardmäßig ab und übernimmt die Einstellung nicht
  aus `.rdp`-Dateien, deshalb wird es nicht verwendet. FreeRDP 3 nutzt mit
  `/gateway:…,type:http` automatisch Websocket.
- NPM braucht für rdpgw:
  - HTTP/2 aus,
  - `proxy_buffering off`, `proxy_request_buffering off`,
    `chunked_transfer_encoding off`,
  - lange Timeouts und Websocket-Header.

  Vorlage: `docs/rdpgw/npm-advanced.conf`.
- rdpgw erlaubt jedes private Ziel auf Port 3389 (Entscheidung A); vmdash fasst
  rdpgw nie an. Eingeschränkt wird per Firewall auf maxwork (3389 nur ins
  VM-Netz) und Authentik-Binding.
- `Hosts` in `rdpgw.yaml` braucht auch bei `HostSelection: any` mindestens einen
  Eintrag, sonst startet rdpgw nicht („Not enough hosts to connect to specified“).
- `client/vmdash-rdp-setup.sh` nicht inhaltlich umbauen; Änderungen nur mit
  Begründung im Commit. Vor jedem Commit `shellcheck` über das Skript und den
  eingebetteten Starter laufen lassen (die CI tut das auch).

## Klonen: Netz und xrdp

- Die DHCP-Reservierung (`networkUpdate`, IP_DHCP_HOST, ADD_LAST, live + config)
  wird nach dem `define` und vor dem ersten Start angelegt. Gewählt wird die
  niedrigste freie IP im DHCP-Bereich. „Klon löschen“ entfernt sie wieder.
- Das xrdp-Zertifikat wird nach dem Hostnamen erneuert, sonst erbt der Klon
  CN `sap-template`. Das passiert nur, wenn `cert.pem` und `key.pem` Symlinks
  auf Snakeoil sind; sonst Abbruch statt Raten.

## Arbeiten am Code

- Tests: `.venv/bin/python -m pytest -q` (venv aus `requirements-dev.txt`).
- Lokal: `VMDASH_BACKEND=mock VMDASH_MOCK_DELAY=0.2 .venv/bin/python -m app`.
- Fehlerpfade im Mock: `VMDASH_MOCK_FAIL_STEP=<schritt>` (Liste in `mock_backend.py`).
- Ein neuer Backend-Aufruf gehört ins Interface, in beide Backends und in einen Test.
- `testdata/sap-template.xml` ist die echte Domain-XML des Templates; die
  XML-Tests laufen dagegen.

## Öffentliches Repo

Das Repo ist öffentlich. Keine echten Kundennamen (Beispiele: `kunde-beispiel`,
`kunde-demo`), keine Sternchen-Echos in echter Länge, keine Login-Sitzungen,
Zugangsdaten oder internen Adressen in Code, Doku oder `testdata/`. Neue
Mitschnitte vor dem Commit entsprechend bereinigen.
