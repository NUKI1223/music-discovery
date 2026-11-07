"""No network access occurs while reading or writing a local catalogue."""

import gzip
import json
import os
import tempfile
from pathlib import Path

from discovery.contracts import Catalog


def _read_json(path: Path):
    """Read JSON; a missing large snapshot falls back to the committed `<name>.gz` copy."""
    if not path.exists():
        compressed = path.with_name(path.name + ".gz")
        if compressed.exists():
            with gzip.open(compressed, "rt", encoding="utf-8") as handle:
                return json.load(handle)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalog(path: str | Path) -> Catalog:
    """Load a catalogue directly, or a manifest with a relative catalog_path."""
    path = Path(path)
    payload = _read_json(path)
    if "catalog_path" in payload:
        target = Path(payload["catalog_path"])
        if not target.is_absolute():
            target = path.parent / target
        payload = _read_json(target)
    return Catalog.model_validate(payload)


def save_catalog(catalog: Catalog, path: str | Path) -> None:
    """Validate and atomically replace a snapshot; a failed write preserves the old one."""
    catalog = Catalog.model_validate(catalog.model_dump())
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(catalog.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
