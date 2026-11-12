#!/usr/bin/env python3
"""Repair garbled or invalid PDF titles in an existing catalog."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import build_library_index as library
import library_store
import library_paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=library_paths.EXPORTS_DIR / "图书总目录.csv",
    )
    parser.add_argument("--root", type=Path, default=library_paths.ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.DATA_DIR,
    )
    args = parser.parse_args()

    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = list(reader.fieldnames or [])

    changes = []
    for row in rows:
        if row.get("格式") != "PDF":
            continue
        old_title = row.get("书名", "")
        new_title = library.sanitize_pdf_title(
            old_title,
            args.root.resolve() / row["相对路径"],
        )
        if new_title and new_title != old_title:
            row["书名"] = new_title
            changes.append(
                {
                    "旧书名": old_title,
                    "新书名": new_title,
                    "相对路径": row["相对路径"],
                }
            )

    temporary = args.catalog.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(args.catalog)
    library_store.sync_catalog(
        args.catalog,
        args.output / "library.db",
        args.root,
    )

    with (args.output / "exports" / "pdf_title_fixes.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["旧书名", "新书名", "相对路径"],
        )
        writer.writeheader()
        writer.writerows(changes)

    books = library.load_books_from_catalog(args.catalog, args.root)
    exact_groups = {book.exact_group for book in books if book.exact_group}
    title_groups = {book.title_group for book in books if book.title_group}
    library.generate_csv_reports(
        args.output,
        books,
        len(exact_groups),
        0,
        len(title_groups),
    )
    library.generate_html(args.output, books)
    print(f"Fixed PDF titles: {len(changes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
