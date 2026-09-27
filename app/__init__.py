"""vmdash – Web-Dashboard für die LUKS-verschlüsselten Kunden-VMs auf maxwork."""

import os

__version__ = os.environ.get("VMDASH_VERSION", "0.1.0")
