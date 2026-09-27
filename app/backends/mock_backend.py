"""Simulation von libvirt für die Entwicklung ohne VMs.

- Boot mit Konsolenausgabe aus testdata/boot-debian13-luks.txt
- Passphrase "falsch" wird abgelehnt (3 Versuche wie cryptroot), sonst Erfolg;
  nach luksRemoveKey gilt nur noch der per luksAddKey hinzugefügte Schlüssel
- Guest-Agent mit kleinem Dateisystem pro Volume (der Klon erbt es)
- VMDASH_MOCK_DELAY: Grundverzögerung in Sekunden
- VMDASH_MOCK_FAIL_STEP: Schritt, der absichtlich fehlschlägt
  (clone_volume, define, unlock_old, agent, luks_add_key, hostname,
  machine_id, ssh_keys, unlock_new, luks_remove_key)
"""

import copy
import logging
import posixpath
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from .base import (
    PAUSED,
    RUNNING,
    SHUTOFF,
    BackendError,
    ConsoleBusy,
    ConsoleClosed,
    NotFound,
    SerialConsole,
    VmBackend,
    VmInfo,
    VolumeInfo,
)

log = logging.getLogger(__name__)

POOL_DIR = "/var/lib/libvirt/images"
MAX_TRIES = 3
GiB = 1024**3

FALLBACK_BOOT = """BdsDxe: starting Boot0003 "debian" from HD(1,GPT,...)/\\EFI\\debian\\shimx64.efi

Please unlock disk vda3_crypt:
cryptsetup: vda3_crypt: set up successfully
[  OK  ] Reached target basic.target - Basic System.

Debian GNU/Linux 13 kunde-beispiel ttyS0

kunde-beispiel login: maxkhl
[  OK  ] Stopped target sysinit.target - System Initialization.
[  OK  ] Reached target umount.target - Unmount All Filesystems.
"""

FALLBACK_XML = """<domain type='kvm'>
  <name>sap-template</name>
  <uuid>00000000-0000-4000-8000-000000000000</uuid>
  <memory unit='KiB'>6291456</memory>
  <vcpu placement='static'>4</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <nvram>/var/lib/libvirt/qemu/nvram/sap-template_VARS.fd</nvram>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='/var/lib/libvirt/images/sap-template.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'><mac address='52:54:00:00:00:01'/><source network='default'/></interface>
    <serial type='pty'><target port='0'/></serial>
    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>
    <graphics type='vnc' port='5913' autoport='no' listen='127.0.0.1' keymap='de'/>
  </devices>
</domain>
"""


class BootScript:
    """Zerlegt den Konsolenmitschnitt in die Phasen des Boots."""

    def __init__(self, text):
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("Escape character"):
                lines = lines[i + 1:]
                break
        prompt_idx = next(i for i, l in enumerate(lines) if "Please unlock disk" in l)
        m = re.search(r"Please unlock disk \S+:", lines[prompt_idx])
        self.prompt = m.group(0)
        self.device = self.prompt[len("Please unlock disk "):-1]
        # Leerzeilen zu Beginn (Bildschirmlöschen von OVMF) weglassen
        self.pre = "".join(lines[:prompt_idx]).lstrip("\n")
        ok_idx = next(i for i, l in enumerate(lines) if "set up successfully" in l)
        login_idx = next(i for i, l in enumerate(lines) if " login:" in l)
        self.post = lines[ok_idx + 1:login_idx]
        self.login_line = lines[login_idx].split(" login:")[0] + " login: "
        self.host_in_capture = self.login_line.split(" login:")[0]
        stop_idx = next(
            (i for i in range(login_idx, len(lines)) if "Stopp" in lines[i]), len(lines)
        )
        self.shutdown = lines[stop_idx:]

    def for_host(self, text, hostname):
        return text.replace(self.host_in_capture, hostname)


class GuestState:
    """Was im Mock "auf der Disk" liegt; wird beim Volume-Klon kopiert."""

    def __init__(self, hostname):
        self.hostname = hostname
        self.fs = {
            "/etc/hosts": (f"127.0.0.1\tlocalhost\n127.0.1.1\t{hostname}\n".encode(), 0o644),
            "/etc/machine-id": (secrets.token_hex(16).encode() + b"\n", 0o444),
            "/etc/ssh/ssh_host_ed25519_key": (b"old-key", 0o600),
        }
        # Wildcard: jede Passphrase außer "falsch" passt, bis luksRemoveKey lief.
        self.any_key_valid = True
        self.keys = set()


class Volume:
    def __init__(self, name, allocation, guest):
        self.name = name
        self.path = posixpath.join(POOL_DIR, name)
        self.allocation = allocation
        self.guest = guest


class MockVm:
    def __init__(self, xml):
        self.xml = xml
        root = ET.fromstring(xml)
        self.name = root.findtext("name")
        self.disk_path = root.find("./devices/disk[@device='disk']/source").get("file")
        self.state = SHUTOFF
        self.console = None
        self.gen = 0  # erhöht bei jedem Stopp/Neustart; alte Timer verfallen
        self.agent_up = False
        self.phase = "off"  # off, booting, prompt, dead, up
        self.tries = 0
        self.boot_count = 0
        self.exec_log = []  # argv aller agent_exec-Aufrufe (für Tests)
        self.key_file_snapshots = []  # Zustand der Keydateien bei cryptsetup (für Tests)
        self._line = bytearray()


class MockSerial(SerialConsole):
    def __init__(self, backend, vm):
        self._backend = backend
        self._vm = vm
        self._q = queue.Queue()
        self._closed = False

    def push(self, data):
        self._q.put(data)

    def eof(self):
        self._q.put(None)

    def read(self, timeout):
        if self._closed:
            raise ConsoleClosed("Konsole geschlossen")
        try:
            data = self._q.get(timeout=max(timeout, 0))
        except queue.Empty:
            return b""
        if data is None:
            self._closed = True
            raise ConsoleClosed("Konsole geschlossen")
        # Bereits vorliegende Stücke zusammenfassen
        while True:
            try:
                more = self._q.get_nowait()
            except queue.Empty:
                break
            if more is None:
                self._q.put(None)
                break
            data += more
        return data

    def write(self, data):
        if self._closed:
            raise ConsoleClosed("Konsole geschlossen")
        self._backend._console_input(self._vm, data)

    def close(self):
        self._closed = True
        with self._backend._lock:
            if self._vm.console is self:
                self._vm.console = None


class MockBackend(VmBackend):
    mode = "mock"

    def __init__(self, cfg):
        self.cfg = cfg
        self.d = max(cfg.mock_delay, 0.0)
        self.fail_step = cfg.mock_fail_step
        self._lock = threading.RLock()
        try:
            text = cfg.mock_boot_capture.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.warning("Mock: %s fehlt, nutze eingebauten Boot-Text", cfg.mock_boot_capture)
            text = FALLBACK_BOOT
        self.boot = BootScript(text)
        try:
            template_xml = cfg.mock_template_xml.read_text(encoding="utf-8")
        except OSError:
            template_xml = FALLBACK_XML
        self.pool_capacity = 120 * GiB
        self.volumes = {}
        self.vms = {}
        self._setup(template_xml)

    # ------------------------------------------------------------ Aufbau
    def _setup(self, template_xml):
        from ..clone import build_clone_xml, parse_xml

        tpl_name = parse_xml(template_xml).findtext("name")
        tpl_vol = posixpath.basename(
            parse_xml(template_xml).find("./devices/disk[@device='disk']/source").get("file")
        )
        self.volumes[tpl_vol] = Volume(tpl_vol, 16 * GiB, GuestState(tpl_name))
        self.vms[tpl_name] = MockVm(template_xml)

        for name, port, running in (("kunde-beispiel", 5910, True), ("kunde-demo", 5911, False)):
            vol = Volume(f"{name}.qcow2", 21 * GiB, GuestState(name))
            self.volumes[vol.name] = vol
            xml = build_clone_xml(template_xml, name, vol.path, port)
            vm = MockVm(self._with_uuid(xml))
            self.vms[name] = vm
            if running:
                vm.state, vm.phase, vm.agent_up, vm.boot_count = RUNNING, "up", True, 1

    @staticmethod
    def _with_uuid(xml):
        root = ET.fromstring(xml)
        if root.find("uuid") is None:
            el = ET.Element("uuid")
            el.text = str(uuid.uuid4())
            root.insert(1, el)
        return ET.tostring(root, encoding="unicode")

    # ------------------------------------------------------------ Helfer
    def _vm(self, name):
        vm = self.vms.get(name)
        if vm is None:
            raise NotFound(f"VM „{name}“ nicht gefunden")
        return vm

    def _guest(self, vm):
        vol = next((v for v in self.volumes.values() if v.path == vm.disk_path), None)
        if vol is None:
            raise BackendError("Disk der VM fehlt")
        return vol.guest

    def _fail(self, step):
        return self.fail_step == step

    def _sleep(self, factor):
        if self.d:
            time.sleep(self.d * factor)

    def _later(self, vm, gen, factor, fn):
        def run():
            self._sleep(factor)
            with self._lock:
                if vm.gen != gen:
                    return
            fn()

        threading.Thread(target=run, daemon=True).start()

    def _emit(self, vm, text):
        with self._lock:
            if vm.console is not None:
                vm.console.push(text.encode("utf-8"))
            # ohne verbundene Konsole geht die Ausgabe verloren (wie beim pty)

    def _power_off(self, vm):
        with self._lock:
            vm.gen += 1
            vm.state, vm.phase, vm.agent_up = SHUTOFF, "off", False
            if vm.console is not None:
                vm.console.eof()
                vm.console = None

    # ------------------------------------------------------------ Boot
    def _boot(self, vm):
        with self._lock:
            vm.gen += 1
            gen = vm.gen
            vm.phase, vm.tries, vm.agent_up = "booting", 0, False
            vm.boot_count += 1

        def run():
            chunks = _split(self.boot.pre, 6)
            for c in chunks:
                self._sleep(3 / len(chunks))
                with self._lock:
                    if vm.gen != gen:
                        return
                self._emit(vm, c)
            with self._lock:
                if vm.gen != gen:
                    return
                vm.phase = "prompt"
            self._emit(vm, self.boot.prompt)

        threading.Thread(target=run, daemon=True).start()

    def _console_input(self, vm, data):
        with self._lock:
            vm._line += data
            if b"\n" not in vm._line:
                return
            line, _, rest = bytes(vm._line).partition(b"\n")
            vm._line = bytearray(rest)
            if vm.phase != "prompt":
                return
            passphrase = line.decode("utf-8", "replace").rstrip("\r")
            guest = self._guest(vm)
            ok = passphrase != "falsch" and (
                guest.any_key_valid or passphrase in guest.keys
            )
            if self._fail("unlock_old") and vm.boot_count == 1:
                ok = False
            if self._fail("unlock_new") and vm.boot_count > 1:
                ok = False
            gen = vm.gen
        self._emit(vm, "*" * len(passphrase) + "\n")
        dev = self.boot.device
        if not ok:
            with self._lock:
                vm.tries += 1
                exceeded = vm.tries >= MAX_TRIES
                vm.phase = "dead" if exceeded else "prompt"
            self._emit(
                vm,
                "No key available with this passphrase.\n"
                f"cryptsetup: ERROR: {dev}: cryptsetup failed, bad password or options?\n",
            )
            if exceeded:
                self._emit(vm, f"cryptsetup: ERROR: {dev}: maximum number of tries exceeded\n")
            else:
                self._emit(vm, "\n" + self.boot.prompt)
            return
        with self._lock:
            vm.phase = "unlocked"
        self._emit(vm, f"cryptsetup: {dev}: set up successfully\n")

        def after_unlock():
            host = guest.hostname
            chunks = _split(self.boot.for_host("".join(self.boot.post), host), 4)
            for c in chunks:
                self._sleep(2 / len(chunks))
                with self._lock:
                    if vm.gen != gen:
                        return
                self._emit(vm, c)
            self._emit(vm, self.boot.for_host(self.boot.login_line, host))
            with self._lock:
                if vm.gen == gen:
                    vm.phase, vm.agent_up = "up", True

        threading.Thread(target=after_unlock, daemon=True).start()

    # ------------------------------------------------------------ Domains
    def list_vms(self):
        with self._lock:
            return [VmInfo(vm.name, vm.state) for vm in sorted(self.vms.values(), key=lambda v: v.name)]

    def get_vm(self, name):
        with self._lock:
            vm = self.vms.get(name)
            return VmInfo(vm.name, vm.state) if vm else None

    def start(self, name, paused=False):
        with self._lock:
            vm = self._vm(name)
            if vm.state != SHUTOFF:
                raise BackendError(f"VM „{name}“ läuft bereits")
            vm.state = PAUSED if paused else RUNNING
        if not paused:
            self._boot(vm)

    def resume(self, name):
        with self._lock:
            vm = self._vm(name)
            if vm.state != PAUSED:
                raise BackendError(f"VM „{name}“ ist nicht angehalten")
            vm.state = RUNNING
        self._boot(vm)

    def shutdown(self, name):
        with self._lock:
            vm = self._vm(name)
            if vm.state == SHUTOFF:
                raise BackendError(f"VM „{name}“ ist bereits aus")
            gen = vm.gen
            vm.agent_up = False
        host = self._guest(vm).hostname
        self._emit(vm, self.boot.for_host("".join(self.boot.shutdown), host))
        self._later(vm, gen, 2, lambda: self._power_off(vm))

    def destroy(self, name):
        with self._lock:
            vm = self._vm(name)
            if vm.state == SHUTOFF:
                raise BackendError(f"VM „{name}“ ist bereits aus")
        self._power_off(vm)

    def reboot(self, name):
        with self._lock:
            vm = self._vm(name)
            if not vm.agent_up:
                raise BackendError("Guest-Agent nicht erreichbar")
            vm.agent_up = False
            vm.gen += 1
            gen = vm.gen
        host = self._guest(vm).hostname

        def run():
            self._emit(vm, self.boot.for_host("".join(self.boot.shutdown), host))
            self._sleep(1)
            with self._lock:
                if vm.gen != gen:
                    return
            self._emit(vm, "reboot: Restarting system\n")
            self._boot(vm)

        threading.Thread(target=run, daemon=True).start()

    def undefine_with_storage(self, name):
        with self._lock:
            vm = self._vm(name)
            if vm.state != SHUTOFF:
                raise BackendError("VM läuft noch")
            del self.vms[name]
            for vname, vol in list(self.volumes.items()):
                if vol.path == vm.disk_path:
                    del self.volumes[vname]

    def open_serial(self, name):
        with self._lock:
            vm = self._vm(name)
            if vm.state == SHUTOFF:
                raise BackendError("VM ist aus")
            if vm.console is not None:
                raise ConsoleBusy("Konsole belegt")
            vm.console = MockSerial(self, vm)
            return vm.console

    def get_inactive_xml(self, name):
        with self._lock:
            return self._vm(name).xml

    def all_domain_xml(self):
        with self._lock:
            return [vm.xml for vm in self.vms.values()]

    def define_xml(self, xml):
        self._sleep(0.5)
        if self._fail("define"):
            raise BackendError("Mock: define absichtlich fehlgeschlagen")
        vm = MockVm(self._with_uuid(xml))
        with self._lock:
            if vm.name in self.vms:
                raise BackendError(f"Domain „{vm.name}“ existiert bereits")
            self.vms[vm.name] = vm

    # ------------------------------------------------------------ Storage
    def pool_free_bytes(self):
        with self._lock:
            used = sum(v.allocation for v in self.volumes.values())
            return max(self.pool_capacity - used, 0)

    def volume_exists(self, vol_name):
        with self._lock:
            return vol_name in self.volumes

    def volume_by_path(self, path):
        with self._lock:
            for v in self.volumes.values():
                if v.path == path:
                    return VolumeInfo(v.name, v.path, v.allocation, self.cfg.storage_pool)
        raise NotFound(f"Kein Volume mit Pfad {path}")

    def clone_volume(self, src_path, new_vol):
        with self._lock:
            src = next((v for v in self.volumes.values() if v.path == src_path), None)
            if src is None:
                raise NotFound(f"Kein Volume mit Pfad {src_path}")
            if new_vol in self.volumes:
                raise BackendError(f"Volume {new_vol} existiert bereits")
        self._sleep(4)
        if self._fail("clone_volume"):
            raise BackendError("Mock: vol-clone absichtlich fehlgeschlagen")
        with self._lock:
            vol = Volume(new_vol, src.allocation, copy.deepcopy(src.guest))
            self.volumes[new_vol] = vol
            return vol.path

    def delete_volume(self, vol_name):
        with self._lock:
            if vol_name not in self.volumes:
                raise NotFound(f"Volume {vol_name} nicht gefunden")
            del self.volumes[vol_name]

    # ------------------------------------------------------------ Agent
    def _agent_vm(self, name):
        vm = self._vm(name)
        if not vm.agent_up or vm.state != RUNNING:
            raise BackendError("Guest-Agent nicht erreichbar")
        return vm

    def agent_ping(self, name):
        with self._lock:
            vm = self.vms.get(name)
            return bool(vm and vm.agent_up and vm.state == RUNNING and not self._fail("agent"))

    def agent_write_file(self, name, path, data):
        self._sleep(0.1)
        with self._lock:
            vm = self._agent_vm(name)
            fs = self._guest(vm).fs
            mode = fs[path][1] if path in fs else 0o644
            fs[path] = (bytes(data), mode)

    def agent_exec(self, name, argv, timeout=120):
        self._sleep(0.2)
        with self._lock:
            vm = self._agent_vm(name)
            vm.exec_log.append(list(argv))
            return self._run(vm, self._guest(vm), list(argv))

    def _run(self, vm, g, argv):
        fs = g.fs
        cmd = argv[0]
        if cmd == "install" and argv[1:4] == ["-m", "600", "/dev/null"]:
            fs[argv[4]] = (b"", 0o600)
        elif cmd == "rm":
            for p in argv[1:]:
                if not p.startswith("-"):
                    fs.pop(p, None)
        elif cmd == "sh" and argv[1:] == ["-c", "rm -f /etc/ssh/ssh_host_*"]:
            for p in [p for p in fs if p.startswith("/etc/ssh/ssh_host_")]:
                del fs[p]
        elif cmd == "hostname":
            return 0, g.hostname + "\n", ""
        elif cmd == "hostnamectl":
            if self._fail("hostname"):
                return 1, "", "Mock: hostnamectl absichtlich fehlgeschlagen"
            g.hostname = argv[2]
        elif cmd == "sed":
            content, mode = fs["/etc/hosts"]
            fs["/etc/hosts"] = (_run_sed(argv[3], content), mode)
        elif cmd == "systemd-machine-id-setup":
            if self._fail("machine_id"):
                return 1, "", "Mock: machine-id absichtlich fehlgeschlagen"
            fs["/etc/machine-id"] = (secrets.token_hex(16).encode() + b"\n", 0o444)
        elif cmd == "test":
            return (0 if argv[1:] == ["-d", "/etc/ssh"] else 1), "", ""
        elif cmd == "ssh-keygen":
            if self._fail("ssh_keys"):
                return 1, "", "Mock: ssh-keygen absichtlich fehlgeschlagen"
            fs["/etc/ssh/ssh_host_ed25519_key"] = (b"new-key-" + secrets.token_hex(4).encode(), 0o600)
        elif cmd == "systemctl":
            return 0, "running\n", ""
        elif cmd == "cryptsetup":
            return self._cryptsetup(vm, g, argv)
        return 0, "", ""

    def _cryptsetup(self, vm, g, argv):
        action = argv[1]
        if action == "isLuks":
            return (0 if argv[2] == self.cfg.luks_device else 1), "", ""
        key_file = argv[argv.index("--key-file") + 1]
        files = [a for a in argv if a.startswith("/run/")]
        vm.key_file_snapshots.append({p: g.fs.get(p) for p in files})
        if key_file not in g.fs:
            return 1, "", f"Failed to open key file {key_file}."
        old = g.fs[key_file][0].decode()
        valid = old != "falsch" and (g.any_key_valid or old in g.keys)
        if action == "luksAddKey":
            if self._fail("luks_add_key") or not valid:
                return 2, "", "No key available with this passphrase."
            g.keys.add(g.fs[argv[-1]][0].decode())
            return 0, "", ""
        if action == "luksRemoveKey":
            if self._fail("luks_remove_key") or not valid:
                return 2, "", "No key available with this passphrase."
            g.any_key_valid = False
            g.keys.discard(old)
            return 0, "", ""
        return 1, "", f"Mock: unbekannte Aktion {action}"


def _split(text, n):
    if not text:
        return [""]
    size = max(len(text) // n, 1)
    return [text[i:i + size] for i in range(0, len(text), size)]


def _run_sed(expr, content):
    """Führt den sed-Ausdruck echt aus (falls sed vorhanden), sonst unverändert."""
    sed = shutil.which("sed")
    if not sed:
        return content
    try:
        return subprocess.run(
            [sed, "-E", "-e", expr], input=content, capture_output=True, check=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return content
