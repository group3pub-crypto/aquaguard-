"""SHA-256 manifest for trained artefacts (``manifest.json`` next to the models).

joblib files are pickles = TRUSTED artefacts. The manifest lets the backend refuse a model file
that changed after training (MODEL_MANIFEST_REQUIRED=true); it does not make an untrusted
pickle safe.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "manifest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def update_manifest(out_dir: Path, names: list[str]) -> dict:
    """Merge the hashes of ``names`` (files in ``out_dir``) into ``manifest.json``."""
    mp = out_dir / MANIFEST
    try:
        files = json.loads(mp.read_text()).get("files", {})
    except (OSError, ValueError):
        files = {}
    for n in names:
        files[n] = sha256_file(out_dir / n)
    files = {k: v for k, v in files.items() if (out_dir / k).is_file()}
    manifest = {
        "algorithm": "sha256",
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": dict(sorted(files.items())),
    }
    mp.write_text(json.dumps(manifest, indent=2))
    return manifest
