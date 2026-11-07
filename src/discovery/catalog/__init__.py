"""Validated local catalogue snapshots and manifest-aware loading."""

from .io import load_catalog, save_catalog


def audit_catalog(*args, **kwargs):
    """Lazy public entry point, also usable through ``python -m ...catalog.audit``."""
    from .audit import audit_catalog as run_audit

    return run_audit(*args, **kwargs)


__all__ = ["load_catalog", "save_catalog", "audit_catalog"]
