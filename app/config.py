"""Konfiguration aus Env-Variablen und der optionalen VM-Konfigurationsdatei."""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BOOT_CAPTURE = ROOT_DIR / "testdata" / "boot-debian13-luks.txt"
DEFAULT_TEMPLATE_XML = ROOT_DIR / "testdata" / "sap-template.xml"

# Meldung von cryptroot (cryptsetup-initramfs), wenn alle Versuche verbraucht sind.
TRIES_EXCEEDED_REGEX = r"cryptsetup: ERROR: \S+: maximum number of tries exceeded"


def _env(name, default):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name, default):
    value = _env(name, None)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} muss eine ganze Zahl sein, ist aber {value!r}")


def _env_float(name, default):
    value = _env(name, None)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{name} muss eine Zahl sein, ist aber {value!r}")


@dataclass
class Config:
    port: int = 8000
    backend: str = "auto"
    libvirt_uri: str = "qemu:///system"
    prompt_regex: str = r"Please unlock disk (\S+):"
    success_regex: str = r"cryptsetup: (\S+): set up successfully"
    fail_regex: str = r"cryptsetup: ERROR: \S+: cryptsetup failed"
    tries_exceeded_regex: str = TRIES_EXCEEDED_REGEX
    prompt_timeout: float = 120
    unlock_timeout: float = 30
    # Wie lange ein Job nach "wrong_key" auf eine neue Eingabe wartet.
    retry_timeout: float = 600
    template_vm: str = "sap-template"
    clone_prefix: str = "kunde-"
    storage_pool: str = "default"
    vnc_port_min: int = 5910
    vnc_port_max: int = 5919
    luks_device: str = "/dev/vda3"
    agent_timeout: float = 180
    vm_config_path: str = ""
    # Nur für das Mock-Backend
    mock_delay: float = 1.0
    mock_fail_step: str = ""
    mock_boot_capture: Path = DEFAULT_BOOT_CAPTURE
    mock_template_xml: Path = DEFAULT_TEMPLATE_XML

    _vm_config_cache: dict = field(default_factory=dict, repr=False)
    _vm_config_mtime: float | None = field(default=None, repr=False)
    _vm_config_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls):
        cfg = cls(
            port=_env_int("VMDASH_PORT", 8000),
            backend=_env("VMDASH_BACKEND", "auto").lower(),
            libvirt_uri=_env("VMDASH_LIBVIRT_URI", "qemu:///system"),
            prompt_regex=_env("VMDASH_PROMPT_REGEX", cls.prompt_regex),
            success_regex=_env("VMDASH_SUCCESS_REGEX", cls.success_regex),
            fail_regex=_env("VMDASH_FAIL_REGEX", cls.fail_regex),
            prompt_timeout=_env_float("VMDASH_PROMPT_TIMEOUT", 120),
            unlock_timeout=_env_float("VMDASH_UNLOCK_TIMEOUT", 30),
            template_vm=_env("VMDASH_TEMPLATE_VM", "sap-template"),
            clone_prefix=_env("VMDASH_CLONE_PREFIX", "kunde-"),
            storage_pool=_env("VMDASH_STORAGE_POOL", "default"),
            vnc_port_min=_env_int("VMDASH_VNC_PORT_MIN", 5910),
            vnc_port_max=_env_int("VMDASH_VNC_PORT_MAX", 5919),
            luks_device=_env("VMDASH_LUKS_DEVICE", "/dev/vda3"),
            agent_timeout=_env_float("VMDASH_AGENT_TIMEOUT", 180),
            vm_config_path=_env("VMDASH_VM_CONFIG", ""),
            mock_delay=_env_float("VMDASH_MOCK_DELAY", 1.0),
            mock_fail_step=_env("VMDASH_MOCK_FAIL_STEP", ""),
        )
        cfg.validate()
        return cfg

    def validate(self):
        if self.backend not in ("auto", "libvirt", "mock"):
            raise ValueError("VMDASH_BACKEND muss auto, libvirt oder mock sein")
        for name in ("prompt_regex", "success_regex", "fail_regex", "tries_exceeded_regex"):
            try:
                re.compile(getattr(self, name))
            except re.error as e:
                raise ValueError(f"Ungültiger regulärer Ausdruck in {name}: {e}")
        if self.vnc_port_min > self.vnc_port_max:
            raise ValueError("VMDASH_VNC_PORT_MIN ist größer als VMDASH_VNC_PORT_MAX")
        if not re.fullmatch(r"[a-z0-9-]*", self.clone_prefix):
            raise ValueError("VMDASH_CLONE_PREFIX darf nur a-z, 0-9 und - enthalten")

    def vm_config(self):
        """Liest VMDASH_VM_CONFIG; bei Änderung der Datei ohne Neustart neu."""
        if not self.vm_config_path:
            return {}
        with self._vm_config_lock:
            try:
                mtime = os.stat(self.vm_config_path).st_mtime
            except OSError as e:
                if self._vm_config_mtime is not None or not self._vm_config_cache:
                    log.warning("VMDASH_VM_CONFIG nicht lesbar: %s", e)
                self._vm_config_cache, self._vm_config_mtime = {}, None
                return {}
            if mtime != self._vm_config_mtime:
                try:
                    with open(self.vm_config_path, encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        raise ValueError("oberste Ebene muss ein Objekt sein")
                    self._vm_config_cache = data
                except (OSError, ValueError) as e:
                    log.error("VMDASH_VM_CONFIG fehlerhaft: %s", e)
                    self._vm_config_cache = {}
                self._vm_config_mtime = mtime
            return self._vm_config_cache

    def rdp_for(self, name):
        """RDP-Eintrag einer VM als (host, port) oder None."""
        entry = self.vm_config().get(name)
        if not isinstance(entry, dict):
            return None
        host = entry.get("rdp_host")
        port = entry.get("rdp_port", 3389)
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            return None
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None
        return host, port
