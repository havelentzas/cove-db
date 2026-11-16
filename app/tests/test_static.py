import hashlib
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "cove" / "web" / "static"


def test_vendored_scripts_match_recorded_checksums():
    """Vendored JS must be exactly the reviewed upstream release (supply-chain check)."""
    lines = [l.split() for l in (STATIC / "CHECKSUMS").read_text().splitlines() if l and not l.startswith("#")]
    assert lines, "CHECKSUMS is empty"
    for digest, name in lines:
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == digest, f"{name} changed"
