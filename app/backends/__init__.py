import logging

log = logging.getLogger(__name__)


def create_backend(cfg):
    """VMDASH_BACKEND: mock, libvirt oder auto (libvirt versuchen, sonst mock)."""
    from .mock_backend import MockBackend

    if cfg.backend == "mock":
        log.warning("MOCK-MODUS: VMDASH_BACKEND=mock, es werden keine echten VMs angesprochen")
        return MockBackend(cfg)
    if cfg.backend == "libvirt":
        from .libvirt_backend import LibvirtBackend

        return LibvirtBackend(cfg)
    try:
        from .libvirt_backend import LibvirtBackend

        return LibvirtBackend(cfg)
    except Exception as e:  # ImportError (kein libvirt-python) oder keine Verbindung
        log.warning("=" * 72)
        log.warning("libvirt nicht erreichbar (%s): %s", cfg.libvirt_uri, e)
        log.warning("FALLE AUF MOCK-MODUS ZURÜCK – es werden KEINE echten VMs angesprochen!")
        log.warning("Für den echten Betrieb VMDASH_BACKEND=libvirt setzen.")
        log.warning("=" * 72)
        return MockBackend(cfg)
