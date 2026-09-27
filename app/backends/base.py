"""Schnittstelle zwischen App und Virtualisierung (libvirt oder Mock)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..network import ip_for_macs, vm_macs

# Vereinheitlichte VM-Zustände
RUNNING = "running"
SHUTOFF = "shutoff"
PAUSED = "paused"
SHUTTING_DOWN = "shutting_down"
CRASHED = "crashed"
SUSPENDED = "suspended"
UNKNOWN = "unknown"


class BackendError(Exception):
    """Fehler mit einer für Nutzer verständlichen Meldung."""


class NotFound(BackendError):
    pass


class ConsoleBusy(BackendError):
    pass


class ConsoleClosed(BackendError):
    pass


@dataclass
class VmInfo:
    name: str
    state: str


@dataclass
class VolumeInfo:
    name: str
    path: str
    allocation: int
    pool: str


class SerialConsole(ABC):
    @abstractmethod
    def read(self, timeout: float) -> bytes:
        """Liefert verfügbare Bytes; b"" wenn innerhalb von timeout nichts kam.

        Wirft ConsoleClosed, wenn die Konsole geschlossen wurde (z. B. VM aus).
        """

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class VmBackend(ABC):
    mode = "abstract"

    # --- Domains ---------------------------------------------------------
    @abstractmethod
    def list_vms(self) -> list[VmInfo]: ...

    @abstractmethod
    def get_vm(self, name: str) -> VmInfo | None: ...

    @abstractmethod
    def start(self, name: str, paused: bool = False) -> None:
        """Startet die VM; mit paused=True angehalten (für open_serial vor dem Boot)."""

    @abstractmethod
    def resume(self, name: str) -> None: ...

    @abstractmethod
    def shutdown(self, name: str) -> None:
        """ACPI-Shutdown."""

    @abstractmethod
    def destroy(self, name: str) -> None:
        """Hart ausschalten."""

    @abstractmethod
    def reboot(self, name: str) -> None:
        """Neustart über den Guest-Agent."""

    @abstractmethod
    def undefine_with_storage(self, name: str) -> None:
        """Domain entfernen inkl. Disk-Volumes und NVRAM-Datei."""

    @abstractmethod
    def open_serial(self, name: str) -> SerialConsole:
        """Öffnet die serielle Konsole. Wirft ConsoleBusy, wenn sie belegt ist."""

    @abstractmethod
    def get_inactive_xml(self, name: str) -> str: ...

    @abstractmethod
    def all_domain_xml(self) -> list[str]:
        """XML aller definierten Domains (inaktiv, bei laufenden zusätzlich live)."""

    @abstractmethod
    def define_xml(self, xml: str) -> None: ...

    # --- Storage ---------------------------------------------------------
    @abstractmethod
    def pool_free_bytes(self) -> int: ...

    @abstractmethod
    def volume_exists(self, vol_name: str) -> bool:
        """Existiert das Volume im konfigurierten Pool?"""

    @abstractmethod
    def volume_by_path(self, path: str) -> VolumeInfo: ...

    @abstractmethod
    def clone_volume(self, src_path: str, new_vol: str) -> str:
        """Klont ein Volume in den konfigurierten Pool (wie virsh vol-clone).

        Liefert den Pfad des neuen Volumes.
        """

    @abstractmethod
    def delete_volume(self, vol_name: str) -> None: ...

    # --- Netz (DHCP im libvirt-Netz VMDASH_LIBVIRT_NETWORK) ---------------
    @abstractmethod
    def dhcp_state(self):
        """DhcpState des konfigurierten Netzes (Bereiche, Reservierungen, Leases)."""

    @abstractmethod
    def add_dhcp_host(self, mac: str, name: str, ip: str) -> None:
        """Feste DHCP-Reservierung anlegen (live und in der Konfiguration)."""

    @abstractmethod
    def remove_dhcp_host(self, mac: str, name: str, ip: str) -> None: ...

    def get_ip(self, name: str, state=None) -> str | None:
        """IP einer VM: DHCP-Reservierung über ihre MAC, sonst aktuelle Lease."""
        state = state or self.dhcp_state()
        return ip_for_macs(state, vm_macs(self.get_inactive_xml(name), state.network))

    # --- Guest-Agent -----------------------------------------------------
    @abstractmethod
    def agent_ping(self, name: str) -> bool: ...

    @abstractmethod
    def agent_write_file(self, name: str, path: str, data: bytes) -> None: ...

    @abstractmethod
    def agent_exec(self, name: str, argv: list[str], timeout: float = 120) -> tuple[int, str, str]:
        """Führt argv im Gast aus (ohne Shell). Liefert (exitcode, stdout, stderr)."""

    def close(self):
        pass
