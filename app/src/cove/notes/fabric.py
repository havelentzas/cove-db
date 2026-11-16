"""Notes on the fabric: its manifest (docs/fabric.md). Capabilities are added in the retrofit."""

from __future__ import annotations

from cove import fabric

MANIFEST = fabric.register(fabric.Manifest(key="notes", version=1, read_scope="notes:read"))
