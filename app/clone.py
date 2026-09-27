"""Klonen einer VM: Host-Seite (Volume, XML, define) und Gast-Seite (Agent).

Ersetzt das Shell-Skript vm-klon. Disk-Kopie über die libvirt-Storage-API
(entspricht virsh vol-clone): virt-clone verweigert Images mit internen
qcow2-Snapshots, vol-clone flacht sie ein.
"""

import io
import logging
import posixpath
import re
import secrets as pysecrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .backends.base import SHUTOFF, BackendError
from .unlock import UnlockFailed, WrongKey, boot_and_unlock, reboot_and_unlock

log = logging.getLogger(__name__)

SUFFIX_RE = re.compile(r"^[a-z0-9-]+$")
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
AGENT_CHANNEL = "org.qemu.guest_agent.0"
SPACE_FACTOR = 1.2
MAX_PASSPHRASE = 512

STEPS = [
    ("check", "Vorbedingungen prüfen"),
    ("clone_volume", "Disk klonen (vol-clone)"),
    ("define", "Domain-XML anpassen und definieren"),
    ("unlock_old", "Klon starten und mit alter Passphrase entsperren"),
    ("agent", "Auf Guest-Agent warten"),
    ("luks_add_key", "Neue Passphrase als Keyslot hinzufügen"),
    ("hostname", "Hostname setzen"),
    ("machine_id", "machine-id neu erzeugen"),
    ("ssh_keys", "SSH-Host-Keys neu erzeugen"),
    ("unlock_new", "Neustart und Entsperren mit neuer Passphrase"),
    ("luks_remove_key", "Alten Keyslot entfernen"),
]

UNLOCK_LABELS = {
    "starting": "VM wird gestartet …",
    "waiting_prompt": "Warte auf LUKS-Prompt …",
    "unlocking": "Passphrase eingegeben, warte auf Ergebnis …",
    "unlocked": "Entsperrt",
    "wrong_key": "Passphrase falsch – bitte erneut eingeben",
}

HINT_KEEP = (
    "Der Klon bleibt bestehen. Mit „Klon löschen“ werden Domain, Volume und "
    "NVRAM-Datei entfernt."
)
HINT_OLD_SLOT = (
    "Der alte Keyslot ist noch aktiv: Der Klon lässt sich weiterhin mit der "
    "Passphrase der Quell-VM entsperren. "
)


class CloneError(Exception):
    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


# --------------------------------------------------------------------- XML


def parse_xml(xml):
    """Parst Domain-XML und registriert deren Namespace-Präfixe (z. B. libosinfo)."""
    for _event, (prefix, uri) in ET.iterparse(io.StringIO(xml), events=("start-ns",)):
        if prefix:
            ET.register_namespace(prefix, uri)
    return ET.fromstring(xml)


def has_agent_channel(root):
    for ch in root.findall("./devices/channel"):
        t = ch.find("target")
        if t is not None and t.get("type") == "virtio" and t.get("name") == AGENT_CHANNEL:
            return True
    return False


def main_disk(root):
    """Die einzige Festplatte (device='disk') der Domain."""
    disks = [d for d in root.findall("./devices/disk") if d.get("device", "disk") == "disk"]
    if len(disks) != 1:
        raise CloneError(
            f"Die Quell-VM hat {len(disks)} Festplatten; unterstützt wird genau eine."
        )
    disk = disks[0]
    src = disk.find("source")
    if disk.get("type") != "file" or src is None or not src.get("file"):
        raise CloneError("Die Festplatte der Quell-VM ist keine Datei (type='file'); nicht unterstützt.")
    return disk


def vnc_ports(root):
    ports = set()
    for g in root.findall("./devices/graphics[@type='vnc']"):
        try:
            port = int(g.get("port", "-1"))
        except ValueError:
            continue
        if port > 0:
            ports.add(port)
    return ports


def free_vnc_port(xmls, lo, hi):
    used = set()
    for xml in xmls:
        used |= vnc_ports(parse_xml(xml))
    for port in range(lo, hi + 1):
        if port not in used:
            return port
    return None


def build_clone_xml(src_xml, new_name, new_disk_path, vnc_port):
    """Passt die inaktive Domain-XML der Quelle für den Klon an."""
    root = parse_xml(src_xml)

    root.find("name").text = new_name
    uuid = root.find("uuid")
    if uuid is not None:
        root.remove(uuid)

    # MAC-Adressen entfernen, libvirt vergibt neue
    for parent in root.iter():
        for mac in parent.findall("mac"):
            if mac.get("address") is not None:
                parent.remove(mac)

    main_disk(root).find("source").set("file", new_disk_path)

    for g in root.findall("./devices/graphics[@type='vnc']"):
        g.set("port", str(vnc_port))
        g.set("autoport", "no")
    # SPICE mit autoport bleibt unverändert.

    nvram = root.find("./os/nvram")
    if nvram is not None and nvram.text and nvram.text.strip():
        src_nvram = nvram.text.strip()
        nvram.set("template", src_nvram)
        nvram.text = posixpath.join(posixpath.dirname(src_nvram), f"{new_name}_VARS.fd")

    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------- Vorbedingungen


@dataclass
class ClonePlan:
    source: str
    target: str
    src_disk_path: str
    vol_name: str
    vnc_port: int
    need_bytes: int
    free_bytes: int


def validate_passphrase(p, what):
    if not isinstance(p, str) or p == "":
        raise CloneError(f"{what} fehlt.")
    if len(p) > MAX_PASSPHRASE:
        raise CloneError(f"{what} ist zu lang (max. {MAX_PASSPHRASE} Zeichen).")
    if any(c in p for c in "\r\n\x00"):
        raise CloneError(f"{what} darf keine Zeilenumbrüche enthalten.")


def check_preconditions(backend, cfg, source, suffix, old_passphrase, new_passphrase):
    if not isinstance(suffix, str) or not SUFFIX_RE.match(suffix):
        raise CloneError("Kundenkürzel: nur Kleinbuchstaben, Ziffern und Bindestrich erlaubt.")
    target = cfg.clone_prefix + suffix
    if not HOSTNAME_RE.match(target):
        raise CloneError(
            f"„{target}“ ist kein gültiger Hostname (max. 63 Zeichen, "
            "nicht mit Bindestrich beginnen oder enden)."
        )
    validate_passphrase(old_passphrase, "Aktuelle Passphrase")
    validate_passphrase(new_passphrase, "Neue Passphrase")
    if old_passphrase == new_passphrase:
        raise CloneError("Die neue Passphrase muss sich von der alten unterscheiden.")

    src = backend.get_vm(source)
    if src is None:
        raise CloneError(f"Quell-VM „{source}“ existiert nicht.")
    if src.state != SHUTOFF:
        raise CloneError(
            f"Quell-VM „{source}“ ist nicht ausgeschaltet. Eine laufende VM zu "
            "kopieren ergibt ein inkonsistentes Dateisystem im Klon. Bitte erst herunterfahren."
        )
    if backend.get_vm(target) is not None:
        raise CloneError(f"Eine VM namens „{target}“ existiert bereits.")
    vol_name = f"{target}.qcow2"
    if backend.volume_exists(vol_name):
        raise CloneError(f"Das Volume „{vol_name}“ existiert bereits im Pool „{cfg.storage_pool}“.")

    root = parse_xml(backend.get_inactive_xml(source))
    if not has_agent_channel(root):
        raise CloneError(
            "Die Quell-VM hat keinen Guest-Agent-Kanal (org.qemu.guest_agent.0). "
            "Auf dem Host ergänzen mit: virt-xml "
            f"{source} --add-device --channel unix,target.type=virtio,"
            "target.name=org.qemu.guest_agent.0 – und im Gast qemu-guest-agent installieren."
        )
    src_disk_path = main_disk(root).find("source").get("file")
    vol = backend.volume_by_path(src_disk_path)
    need = int(vol.allocation * SPACE_FACTOR)
    free = backend.pool_free_bytes()
    if free < need:
        raise CloneError(
            f"Zu wenig Platz im Pool „{cfg.storage_pool}“: benötigt ca. {_gb(need)} "
            f"(belegte Größe der Quell-Disk + 20 %), frei {_gb(free)}."
        )
    port = free_vnc_port(backend.all_domain_xml(), cfg.vnc_port_min, cfg.vnc_port_max)
    if port is None:
        raise CloneError(
            f"Kein freier VNC-Port im Bereich {cfg.vnc_port_min}–{cfg.vnc_port_max}."
        )
    return ClonePlan(source, target, src_disk_path, vol_name, port, need, free)


def _gb(n):
    return f"{n / 1024**3:.1f} GB"


# ------------------------------------------------------------ Gast-Seite


def _exec(backend, vm, argv, timeout=120):
    try:
        rc, out, err = backend.agent_exec(vm, argv, timeout=timeout)
    except BackendError as e:
        raise CloneError(f"„{argv[0]}“ im Gast fehlgeschlagen: {e}")
    if rc != 0:
        detail = (err or out or "").strip()[:300]
        raise CloneError(f"„{' '.join(argv[:2])}“ im Gast endete mit Code {rc}: {detail}")
    return out


def _write_secret(backend, vm, path, secret):
    # Erst leere Datei mit 0600 anlegen: guest-file-write legt sonst mit 0644 an.
    _exec(backend, vm, ["install", "-m", "600", "/dev/null", path])
    data = bytearray(secret.encode("utf-8"))  # ohne Zeilenumbruch am Ende
    try:
        backend.agent_write_file(vm, path, bytes(data))
    except BackendError:
        # Originalmeldung verwerfen, sie könnte Teile der Anfrage enthalten.
        raise CloneError(f"Schreiben von {path} im Gast fehlgeschlagen.")
    finally:
        for i in range(len(data)):
            data[i] = 0


def _remove_files(backend, vm, paths):
    try:
        rc, _out, _err = backend.agent_exec(vm, ["rm", "-f", *paths], timeout=30)
        ok = rc == 0
    except BackendError:
        ok = False
    if not ok:
        log.error("Schlüsseldateien im Gast %s konnten nicht gelöscht werden", vm)
    return ok


def _secret_path():
    return f"/run/vmdash-{pysecrets.token_hex(8)}.key"


def wait_for_agent(backend, vm, timeout, cancel_event):
    deadline = time.monotonic() + timeout
    while True:
        if backend.agent_ping(vm):
            return
        if cancel_event.is_set():
            raise CloneError("Vorgang abgebrochen.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CloneError(
                f"Der Guest-Agent antwortet nicht innerhalb von {timeout:g} s. "
                "Ist qemu-guest-agent im Gast installiert und aktiv?"
            )
        cancel_event.wait(min(2.0, remaining))


def _hosts_sed_expr(old, new):
    # Ersetzt old als ganzes Wort (begrenzt durch Leerraum, Punkt oder Zeilenrand);
    # die Schleife (:a … ta) erfasst auch mehrere Vorkommen in einer Zeile.
    # old und new bestehen nur aus [A-Za-z0-9-] (vorher geprüft), kein Escaping nötig.
    return rf":a;s/(^|[[:space:]]){old}([[:space:].]|$)/\1{new}\2/;ta"


# ----------------------------------------------------------------- Ablauf


def run_clone(backend, cfg, manager, job, plan, secrets):
    """Führt den Klon aus. secrets: {"old": ..., "new": ...}, wird geleert."""
    vm = plan.target
    old_slot_active = False
    try:
        job.set_state("running")
        job.complete(
            "check",
            f"Ziel {vm}, VNC-Port {plan.vnc_port}, benötigt {_gb(plan.need_bytes)}, "
            f"frei {_gb(plan.free_bytes)}",
        )

        # 1. Disk klonen
        job.begin("clone_volume", f"{posixpath.basename(plan.src_disk_path)} → {plan.vol_name} (kann dauern)")
        if job.cancel_event.is_set():
            raise CloneError("Vorgang abgebrochen.")
        try:
            new_path = backend.clone_volume(plan.src_disk_path, plan.vol_name)
        except BackendError as e:
            raise CloneError(f"Disk klonen fehlgeschlagen: {e}")
        job.set_result(volume_created=True, volume=plan.vol_name)
        job.complete("clone_volume", f"{plan.vol_name} angelegt")

        # 2./3. XML anpassen und definieren
        job.begin("define")
        xml = build_clone_xml(backend.get_inactive_xml(plan.source), vm, new_path, plan.vnc_port)
        try:
            backend.define_xml(xml)
        except BackendError as e:
            raise CloneError(f"Domain definieren fehlgeschlagen: {e}")
        job.set_result(defined=True, vnc_port=plan.vnc_port)
        job.complete("define", f"Domain {vm} definiert, VNC-Port {plan.vnc_port}")
        # Ab hier wird die Quelle nicht mehr gebraucht.
        manager.release(job, plan.source)

        # 4. Mit alter Passphrase entsperren
        job.begin("unlock_old")

        def on_old_state(state):
            job.update("unlock_old", UNLOCK_LABELS.get(state, state))
            job.set_state("wrong_key" if state == "wrong_key" else "running")

        try:
            secrets["old"] = boot_and_unlock(
                backend, cfg, vm, secrets["old"], on_old_state,
                ask_again=lambda: job.wait_for_passphrase(cfg.retry_timeout),
                cancel_event=job.cancel_event,
            )
        except UnlockFailed as e:
            raise CloneError(str(e))
        if secrets["old"] == secrets["new"]:
            raise CloneError("Die neue Passphrase entspricht der alten; Abbruch.")
        job.complete("unlock_old", "Entsperrt")

        # 5. Guest-Agent
        _wait_agent_step(backend, cfg, job, vm)

        # 6. Neue Passphrase als zusätzlichen Keyslot
        job.begin("luks_add_key")
        dev = cfg.luks_device
        rc, _o, _e = backend.agent_exec(vm, ["cryptsetup", "isLuks", dev], timeout=30)
        if rc != 0:
            raise CloneError(f"{dev} ist im Gast kein LUKS-Gerät (VMDASH_LUKS_DEVICE prüfen).")
        old_f, new_f = _secret_path(), _secret_path()
        try:
            _write_secret(backend, vm, old_f, secrets["old"])
            _write_secret(backend, vm, new_f, secrets["new"])
            _exec(backend, vm, ["cryptsetup", "luksAddKey", "--key-file", old_f, dev, new_f], timeout=300)
        finally:
            _remove_files(backend, vm, [old_f, new_f])
        old_slot_active = True
        job.complete("luks_add_key", f"Keyslot auf {dev} hinzugefügt")

        # 7. Hostname
        job.begin("hostname")
        old_host = _exec(backend, vm, ["hostname"]).strip().split(".")[0]
        _exec(backend, vm, ["hostnamectl", "set-hostname", vm])
        if old_host and old_host != vm and re.fullmatch(r"[A-Za-z0-9-]+", old_host):
            _exec(backend, vm, ["sed", "-i", "-E", _hosts_sed_expr(old_host, vm), "/etc/hosts"])
            job.complete("hostname", f"{old_host} → {vm} (auch in /etc/hosts)")
        else:
            job.complete("hostname", f"→ {vm}")

        # 8. machine-id
        job.begin("machine_id")
        _exec(backend, vm, ["rm", "-f", "/etc/machine-id"])
        _exec(backend, vm, ["systemd-machine-id-setup"])
        job.complete("machine_id", "neu erzeugt")

        # 9. SSH-Host-Keys
        job.begin("ssh_keys")
        rc, _o, _e = backend.agent_exec(vm, ["test", "-d", "/etc/ssh"], timeout=30)
        if rc != 0:
            job.complete("ssh_keys", "kein /etc/ssh im Gast – übersprungen", status="skipped")
        else:
            _exec(backend, vm, ["sh", "-c", "rm -f /etc/ssh/ssh_host_*"])
            _exec(backend, vm, ["ssh-keygen", "-A"])
            job.complete("ssh_keys", "neu erzeugt")

        # 10. Neustart, mit neuer Passphrase entsperren
        job.begin("unlock_new", "Neustart über den Guest-Agent …")

        def on_new_state(state):
            job.update("unlock_new", UNLOCK_LABELS.get(state, state))

        try:
            reboot_and_unlock(backend, cfg, vm, secrets["new"], on_new_state, job.cancel_event)
        except WrongKey:
            raise CloneError("Die neue Passphrase entsperrt den Klon nicht. Abbruch.")
        except UnlockFailed as e:
            raise CloneError(f"Entsperren mit der neuen Passphrase fehlgeschlagen: {e}")
        except BackendError as e:
            raise CloneError(f"Neustart fehlgeschlagen: {e}")
        secrets.pop("new", None)
        job.complete("unlock_new", "Mit neuer Passphrase entsperrt")

        # 11. Alten Keyslot entfernen
        _wait_agent_step(backend, cfg, job, vm, step="luks_remove_key")
        old_f = _secret_path()
        try:
            _write_secret(backend, vm, old_f, secrets["old"])
            _exec(backend, vm, ["cryptsetup", "luksRemoveKey", "--key-file", old_f, dev], timeout=300)
        finally:
            _remove_files(backend, vm, [old_f])
        old_slot_active = False
        job.complete("luks_remove_key", "Alter Keyslot entfernt")

        job.finish("done")
    except Exception as e:
        if isinstance(e, (CloneError, BackendError)):
            message = str(e)
        else:
            log.exception("Klon %s: unerwarteter Fehler", vm)
            message = "Interner Fehler, Details im Log des Containers."
        hint = getattr(e, "hint", None) or ""
        if old_slot_active:
            hint = HINT_OLD_SLOT + hint
        created = bool(job.result.get("volume_created"))
        if created:
            hint = (hint + " " + HINT_KEEP).strip()
        job.set_result(deletable=created)
        job.fail(message, hint or None)
    finally:
        # 12. Passphrasen verwerfen, auch bei Abbruch
        secrets.clear()


def _wait_agent_step(backend, cfg, job, vm, step="agent"):
    job.begin(step, "Warte auf Guest-Agent …")
    wait_for_agent(backend, vm, cfg.agent_timeout, job.cancel_event)
    # Warten, bis der Boot fertig ist (dbus/hostnamed nötig); Exitcode egal
    # ("degraded" ist auch fertig).
    try:
        backend.agent_exec(vm, ["systemctl", "is-system-running", "--wait"], timeout=cfg.agent_timeout)
    except BackendError:
        pass
    if step == "agent":
        job.complete(step, "Guest-Agent antwortet")
