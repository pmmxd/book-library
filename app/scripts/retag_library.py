#!/usr/bin/env python3
"""Recompute smart shelves and content tags for an existing catalog."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
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
    if "标签" not in fields:
        fields.insert(fields.index("建议书架") if "建议书架" in fields else len(fields), "标签")
    if "智能子类" not in fields:
        fields.insert(fields.index("建议书架") + 1, "智能子类")

    shelf_counts = Counter()
    subcategory_counts = Counter()
    tag_counts = Counter()
    for row in rows:
        shelf, smart_subcategory, tags = library.classify_book(
            row.get("书名", ""),
            row.get("作者", ""),
            row.get("相对路径", ""),
        )
        row["建议书架"] = shelf
        row["智能子类"] = smart_subcategory
        row["标签"] = tags
        shelf_counts[shelf] += 1
        subcategory_counts[(shelf, smart_subcategory)] += 1
        tag_counts.update(tag for tag in tags.split("；") if tag)

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

    with (args.output / "exports" / "智能分类统计.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["类型", "名称", "书籍数"])
        for name, count in shelf_counts.most_common():
            writer.writerow(["分类", name, count])
        for (shelf, name), count in subcategory_counts.most_common():
            writer.writerow(["二级分类", f"{shelf} / {name}", count])
        for name, count in tag_counts.most_common():
            writer.writerow(["标签", name, count])

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
    print(
        f"Reclassified {len(books)} books with {len(subcategory_counts)} "
        f"subcategories and {len(tag_counts)} tags"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
