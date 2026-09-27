"""Echte Implementierung über libvirt-python. Alles läuft über den libvirt-Socket."""

import base64
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET

from .base import (
    CRASHED,
    PAUSED,
    RUNNING,
    SHUTOFF,
    SHUTTING_DOWN,
    SUSPENDED,
    UNKNOWN,
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

_event_loop_started = False
_event_loop_lock = threading.Lock()


def _start_event_loop(libvirt):
    """Nicht-blockierende Streams (Konsole) und Keepalive brauchen eine Event-Loop."""
    global _event_loop_started
    with _event_loop_lock:
        if _event_loop_started:
            return
        libvirt.virEventRegisterDefaultImpl()

        def run():
            while True:
                try:
                    libvirt.virEventRunDefaultImpl()
                except Exception:
                    log.exception("libvirt-Event-Loop")
                    time.sleep(1)

        threading.Thread(target=run, name="libvirt-events", daemon=True).start()
        _event_loop_started = True


class LibvirtSerial(SerialConsole):
    def __init__(self, libvirt, stream):
        self._libvirt = libvirt
        self._stream = stream
        self._closed = False

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self._closed:
                raise ConsoleClosed("Konsole geschlossen")
            try:
                data = self._stream.recv(4096)
            except self._libvirt.libvirtError as e:
                raise ConsoleClosed(f"Konsole geschlossen: {e.get_error_message()}")
            if data == -2:  # würde blockieren
                if time.monotonic() >= deadline:
                    return b""
                time.sleep(0.05)
                continue
            if not data:
                raise ConsoleClosed("Konsole geschlossen (EOF)")
            return bytes(data)

    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 10
        while view:
            try:
                n = self._stream.send(bytes(view))
            except self._libvirt.libvirtError as e:
                raise ConsoleClosed(f"Konsole geschlossen: {e.get_error_message()}")
            if n == -2:
                if time.monotonic() > deadline:
                    raise ConsoleClosed("Schreiben in die Konsole hängt")
                time.sleep(0.05)
                continue
            view = view[n:]

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.abort()  # wie virsh console beim Trennen
        except self._libvirt.libvirtError:
            pass


class LibvirtBackend(VmBackend):
    mode = "libvirt"

    def __init__(self, cfg):
        import libvirt  # erst hier, damit Tests und Mock ohne libvirt-python laufen
        import libvirt_qemu

        self.libvirt = libvirt
        self.libvirt_qemu = libvirt_qemu
        self.cfg = cfg
        self._lock = threading.Lock()
        _start_event_loop(libvirt)
        libvirt.registerErrorHandler(lambda _ctx, _err: None, None)  # kein stderr-Spam
        self._conn = None
        self._connect()

    # ------------------------------------------------------------ Verbindung
    def _connect(self):
        try:
            conn = self.libvirt.open(self.cfg.libvirt_uri)
        except self.libvirt.libvirtError as e:
            raise BackendError(f"Keine Verbindung zu {self.cfg.libvirt_uri}: {e.get_error_message()}")
        try:
            conn.setKeepAlive(5, 3)
        except self.libvirt.libvirtError:
            pass
        self._conn = conn
        log.info("Verbunden mit %s", self.cfg.libvirt_uri)

    def conn(self):
        with self._lock:
            if self._conn is None or not self._conn.isAlive():
                log.warning("libvirt-Verbindung verloren, verbinde neu")
                self._connect()
            return self._conn

    def _dom(self, name):
        try:
            return self.conn().lookupByName(name)
        except self.libvirt.libvirtError as e:
            if e.get_error_code() == self.libvirt.VIR_ERR_NO_DOMAIN:
                raise NotFound(f"VM „{name}“ nicht gefunden")
            raise self._err(e)

    def _err(self, e):
        return BackendError(e.get_error_message() or str(e))

    def _pool(self):
        try:
            return self.conn().storagePoolLookupByName(self.cfg.storage_pool)
        except self.libvirt.libvirtError as e:
            raise BackendError(f"Storage-Pool „{self.cfg.storage_pool}“: {e.get_error_message()}")

    def _state(self, dom):
        lv = self.libvirt
        state, _reason = dom.state()
        return {
            lv.VIR_DOMAIN_RUNNING: RUNNING,
            lv.VIR_DOMAIN_BLOCKED: RUNNING,
            lv.VIR_DOMAIN_PAUSED: PAUSED,
            lv.VIR_DOMAIN_SHUTDOWN: SHUTTING_DOWN,
            lv.VIR_DOMAIN_SHUTOFF: SHUTOFF,
            lv.VIR_DOMAIN_CRASHED: CRASHED,
            lv.VIR_DOMAIN_PMSUSPENDED: SUSPENDED,
        }.get(state, UNKNOWN)

    # ------------------------------------------------------------ Domains
    def list_vms(self):
        try:
            doms = self.conn().listAllDomains(0)
        except self.libvirt.libvirtError as e:
            raise self._err(e)
        out = []
        for d in doms:
            try:
                out.append(VmInfo(d.name(), self._state(d)))
            except self.libvirt.libvirtError:
                continue  # zwischenzeitlich entfernt
        return sorted(out, key=lambda v: v.name)

    def get_vm(self, name):
        try:
            d = self._dom(name)
            return VmInfo(d.name(), self._state(d))
        except NotFound:
            return None
        except self.libvirt.libvirtError as e:
            raise self._err(e)

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except self.libvirt.libvirtError as e:
            raise self._err(e)

    def start(self, name, paused=False):
        flags = self.libvirt.VIR_DOMAIN_START_PAUSED if paused else 0
        self._call(self._dom(name).createWithFlags, flags)

    def resume(self, name):
        self._call(self._dom(name).resume)

    def shutdown(self, name):
        self._call(self._dom(name).shutdownFlags, self.libvirt.VIR_DOMAIN_SHUTDOWN_ACPI_POWER_BTN)

    def destroy(self, name):
        self._call(self._dom(name).destroy)

    def reboot(self, name):
        self._call(self._dom(name).reboot, self.libvirt.VIR_DOMAIN_REBOOT_GUEST_AGENT)

    def undefine_with_storage(self, name):
        lv = self.libvirt
        dom = self._dom(name)
        if self._state(dom) != SHUTOFF:
            raise BackendError("Die VM läuft noch")
        root = ET.fromstring(self._call(dom.XMLDesc, lv.VIR_DOMAIN_XML_INACTIVE))
        paths = [
            s.get("file")
            for s in root.findall("./devices/disk[@device='disk']/source")
            if s.get("file")
        ]
        # Volumes zuerst nachschlagen, gelöscht wird nach dem undefine
        vols = []
        for p in paths:
            try:
                vols.append(self.conn().storageVolLookupByPath(p))
            except lv.libvirtError:
                log.warning("Volume %s nicht im Pool gefunden, bleibt liegen", p)
        flags = lv.VIR_DOMAIN_UNDEFINE_NVRAM | lv.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE
        flags |= lv.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
        flags |= getattr(lv, "VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA", 0)
        flags |= getattr(lv, "VIR_DOMAIN_UNDEFINE_TPM", 0)
        self._call(dom.undefineFlags, flags)
        for v in vols:
            try:
                v.delete(0)
            except lv.libvirtError as e:
                raise BackendError(f"Domain entfernt, Volume {v.name()} aber nicht: {e.get_error_message()}")

    def open_serial(self, name):
        lv = self.libvirt
        dom = self._dom(name)
        stream = self._call(self.conn().newStream, lv.VIR_STREAM_NONBLOCK)
        try:
            # Flags 0: eine bestehende Sitzung wird NICHT übernommen.
            dom.openConsole(None, stream, 0)
        except lv.libvirtError as e:
            try:
                stream.abort()
            except lv.libvirtError:
                pass
            msg = e.get_error_message() or ""
            # libvirt: "operation failed: Active console session exists for this domain"
            if "console session exists" in msg.lower():
                raise ConsoleBusy(msg)
            raise self._err(e)
        return LibvirtSerial(lv, stream)

    def get_inactive_xml(self, name):
        return self._call(self._dom(name).XMLDesc, self.libvirt.VIR_DOMAIN_XML_INACTIVE)

    def all_domain_xml(self):
        lv = self.libvirt
        out = []
        for d in self._call(self.conn().listAllDomains, 0):
            try:
                out.append(d.XMLDesc(lv.VIR_DOMAIN_XML_INACTIVE))
                if d.isActive():
                    out.append(d.XMLDesc(0))  # enthält per autoport vergebene Ports
            except lv.libvirtError:
                continue
        return out

    def define_xml(self, xml):
        self._call(self.conn().defineXML, xml)

    # ------------------------------------------------------------ Storage
    def pool_free_bytes(self):
        pool = self._pool()
        try:
            pool.refresh(0)
        except self.libvirt.libvirtError:
            pass  # z. B. während eines laufenden vol-clone; dann gilt der letzte Stand
        return self._call(pool.info)[3]

    def volume_exists(self, vol_name):
        pool = self._pool()
        try:
            pool.storageVolLookupByName(vol_name)
            return True
        except self.libvirt.libvirtError as e:
            if e.get_error_code() == self.libvirt.VIR_ERR_NO_STORAGE_VOL:
                return False
            raise self._err(e)

    def volume_by_path(self, path):
        try:
            vol = self.conn().storageVolLookupByPath(path)
        except self.libvirt.libvirtError as e:
            raise NotFound(f"Kein libvirt-Volume für {path}: {e.get_error_message()}")
        _type, _cap, alloc = self._call(vol.info)
        return VolumeInfo(vol.name(), vol.path(), alloc, vol.storagePoolLookupByVolume().name())

    def clone_volume(self, src_path, new_vol):
        """Wie virsh vol-clone: XML der Quelle mit neuem Namen, createXMLFrom."""
        lv = self.libvirt
        try:
            src = self.conn().storageVolLookupByPath(src_path)
        except lv.libvirtError as e:
            raise NotFound(f"Quell-Volume {src_path}: {e.get_error_message()}")
        root = ET.fromstring(self._call(src.XMLDesc, 0))
        root.find("name").text = new_vol
        key = root.find("key")
        if key is not None:
            root.remove(key)
        target = root.find("target")
        path = target.find("path") if target is not None else None
        if path is not None:
            target.remove(path)
        xml = ET.tostring(root, encoding="unicode")
        vol = self._call(self._pool().createXMLFrom, xml, src, 0)
        return vol.path()

    def delete_volume(self, vol_name):
        try:
            vol = self._pool().storageVolLookupByName(vol_name)
        except self.libvirt.libvirtError as e:
            raise NotFound(f"Volume {vol_name}: {e.get_error_message()}")
        self._call(vol.delete, 0)

    # ------------------------------------------------------------ Agent
    def _agent(self, name, command, arguments=None, timeout=10):
        payload = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        try:
            reply = self.libvirt_qemu.qemuAgentCommand(self._dom(name), json.dumps(payload), int(timeout), 0)
        except self.libvirt.libvirtError as e:
            # Nur den Befehlsnamen nennen, nie die Argumente (Dateiinhalt!)
            raise BackendError(f"Guest-Agent-Befehl {command} fehlgeschlagen: {e.get_error_message()}")
        return json.loads(reply).get("return")

    def agent_ping(self, name):
        try:
            self._agent(name, "guest-ping", timeout=5)
            return True
        except BackendError:
            return False

    def agent_write_file(self, name, path, data):
        handle = self._agent(name, "guest-file-open", {"path": path, "mode": "w"})
        try:
            b64 = base64.b64encode(data).decode("ascii")
            self._agent(name, "guest-file-write", {"handle": handle, "buf-b64": b64})
        finally:
            self._agent(name, "guest-file-close", {"handle": handle})

    def agent_exec(self, name, argv, timeout=120):
        ret = self._agent(
            name, "guest-exec", {"path": argv[0], "arg": argv[1:], "capture-output": True}
        )
        pid = ret["pid"]
        deadline = time.monotonic() + timeout
        while True:
            st = self._agent(name, "guest-exec-status", {"pid": pid})
            if st.get("exited"):
                out = base64.b64decode(st.get("out-data", "")).decode("utf-8", "replace")
                err = base64.b64decode(st.get("err-data", "")).decode("utf-8", "replace")
                code = st.get("exitcode", -1 if st.get("signal") else 0)
                return code, out, err
            if time.monotonic() > deadline:
                raise BackendError(f"„{argv[0]}“ im Gast läuft länger als {timeout:g} s")
            time.sleep(0.3)

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except self.libvirt.libvirtError:
                pass
