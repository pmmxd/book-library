#!/usr/bin/env python3
"""One-time migration from catalog CSV/JSON caches to library.db."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import library_store
import library_paths


def main() -> int:
    root = library_paths.ROOT
    data = library_paths.DATA_DIR
    database = library_paths.DB_PATH
    catalog = library_paths.EXPORTS_DIR / "图书总目录.csv"
    ai_json = library_paths.LEGACY_DIR / "ai_summaries.json"
    exports = library_paths.EXPORTS_DIR
    exports.mkdir(parents=True, exist_ok=True)

    if database.exists():
        backup = data / f"library.db.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(database, backup)
    else:
        backup = None

    counts = library_store.sync_catalog(catalog, database, root)
    imported_ai = library_store.import_ai_summaries(database, ai_json)
    exported = library_store.export_catalog_csv(
        database,
        exports / "图书总目录.csv",
    )
    report = {
        "database": str(database),
        "backup": str(backup) if backup else None,
        "counts": counts,
        "ai_imported": imported_ai,
        "csv_exported_rows": exported,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (data / "migration_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
