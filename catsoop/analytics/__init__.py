"""
Learning analytics for CAT-SOOP.

A read-only pipeline that turns CAT-SOOP's own activity logs into
instructor-facing metrics, and an admin-only dashboard page that renders them.

Layers, each usable on its own:

    extract    -- the only module that knows CAT-SOOP's on-disk log format
    normalize  -- raw records -> Event / Anomaly
    store      -- SQLite schema, loader, and incremental cursors
    sync       -- orchestration (cron, CLI, or inline from the page)
    engine     -- course / module / question / student metrics
    render     -- charts and tables as self-contained HTML

CAT-SOOP's data root is only ever read, never written.
"""

from . import extract, normalize, store, sync, engine  # noqa: F401

__all__ = ["extract", "normalize", "store", "sync", "engine", "render"]
