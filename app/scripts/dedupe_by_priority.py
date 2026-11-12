#!/usr/bin/env python3
"""Plan or apply high-confidence duplicate cleanup with format priorities."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import build_library_index as library
import library_store
import library_paths


JUNK_TITLES = {
    "index",
    "bingdian001com",
    "ssreaderprint",
    "untitled",
    "print",
    "全页传真图片",
}
UNKNOWN_AUTHORS = {"", "unknown", "未知", "作者未知"}


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def is_scanned(row: dict) -> bool:
    text = f"{row.get('书名', '')} {row.get('相对路径', '')}".casefold()
    return any(marker in text for marker in ("扫描", "影印", "传真", "ssreader"))


def is_refined(row: dict) -> bool:
    text = f"{row.get('书名', '')} {row.get('相对路径', '')}"
    return any(marker in text for marker in ("精校", "精校版", "校订", "校对版"))


def format_rank(row: dict) -> int:
    extension = row.get("格式", "").upper()
    if extension == "EPUB":
        return 0
    if extension in {"AZW3", "AZW"}:
        return 1
    if extension == "MOBI":
        return 2
    if extension == "PDF":
        return 3 if not is_scanned(row) else 4
    if extension == "TXT":
        return 5
    return 6


def keep_rank(row: dict) -> tuple:
    flags = row.get("质量标记", "")
    penalty_flags = (
        "疑似副本/临时名",
        "位于临时目录",
        "带下载编号",
        "带下载站水印",
        "文件名过长",
        "空文件",
    )
    penalty = sum(flag in flags for flag in penalty_flags)
    collection_rank = {
        "【00总文库】": 0,
        "新书": 3,
        "精简-移动设备": 4,
    }.get(row.get("现有总库", ""), 1)
    return (
        format_rank(row),
        0 if is_refined(row) else 1,
        penalty,
        collection_rank,
        len(row.get("相对路径", "")),
        row.get("相对路径", "").casefold(),
    )


def keeper_reason(keeper: dict, extra: dict) -> str:
    if keeper.get("格式") == "EPUB" and extra.get("格式") != "EPUB":
        return "EPUB格式优先"
    if (
        keeper.get("格式") == "PDF"
        and extra.get("格式") == "PDF"
        and not is_scanned(keeper)
        and is_scanned(extra)
    ):
        return "非扫描版PDF优先"
    if is_refined(keeper) and not is_refined(extra):
        return "精校版优先"
    if keeper.get("格式") == extra.get("格式"):
        return "同格式保留主库/路径更规范版本"
    return "保留格式优先级更高版本"


def build_components(rows: list[dict]) -> list[list[int]]:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    exact_groups: dict[str, list[int]] = defaultdict(list)
    title_groups: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        if row.get("精确重复组"):
            exact_groups[row["精确重复组"]].append(index)
        title = norm(row.get("书名", ""))
        author = norm(row.get("作者", ""))
        if (
            row.get("疑似同名组")
            and author
            and author not in UNKNOWN_AUTHORS
            and title not in JUNK_TITLES
        ):
            title_groups[row["疑似同名组"]].append(index)

    for group in exact_groups.values():
        for index in group[1:]:
            union(group[0], index)
    for group in title_groups.values():
        for index in group[1:]:
            union(group[0], index)

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        components[find(index)].append(index)
    return [group for group in components.values() if len(group) > 1]


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
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = args.root.resolve()
    output = args.output.resolve()
    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = list(reader.fieldnames or [])

    components = build_components(rows)
    plans: list[dict] = []
    keep_indexes: set[int] = set()
    for component in components:
        keeper_index = min(component, key=lambda index: keep_rank(rows[index]))
        keeper = rows[keeper_index]
        keep_indexes.add(keeper_index)
        for index in component:
            if index == keeper_index:
                continue
            extra = rows[index]
            plans.append(
                {
                    "处理动作": "保留",
                    "书名": keeper.get("书名", ""),
                    "作者": keeper.get("作者", ""),
                    "格式": keeper.get("格式", ""),
                    "保留原因": "组内优先级最高",
                    "相对路径": keeper.get("相对路径", ""),
                }
            )
            plans.append(
                {
                    "处理动作": "移入回收站",
                    "书名": extra.get("书名", ""),
                    "作者": extra.get("作者", ""),
                    "格式": extra.get("格式", ""),
                    "保留原因": keeper_reason(keeper, extra),
                    "相对路径": extra.get("相对路径", ""),
                }
            )

    plan_path = output / "exports" / "priority_dedupe_plan.csv"
    with plan_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["处理动作", "书名", "作者", "格式", "保留原因", "相对路径"],
        )
        writer.writeheader()
        writer.writerows(plans)

    extras = [item for item in plans if item["处理动作"] == "移入回收站"]
    reclaim_mb = sum(
        float(rows[index].get("大小MB") or 0)
        for index, row in enumerate(rows)
        if index not in keep_indexes
        and any(item["相对路径"] == row.get("相对路径") for item in extras)
    )
    summary = {
        "groups": len(components),
        "keep": len(keep_indexes),
        "move": len(extras),
        "reclaim_gb": round(reclaim_mb / 1024, 2),
        "applied": args.apply,
        "plan": str(plan_path),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not args.apply:
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash_root = output / "trash" / f"dedupe-{stamp}"
    moved_paths: set[str] = set()
    failed: list[dict] = []
    for item in extras:
        source = (root / item["相对路径"]).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            failed.append({"path": item["相对路径"], "error": "文件不存在或路径无效"})
            continue
        destination = trash_root / item["相对路径"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(destination))
            moved_paths.add(item["相对路径"].replace("/", "\\").casefold())
        except OSError as error:
            failed.append({"path": item["相对路径"], "error": str(error)})

    kept_rows = [
        row
        for row in rows
        if row.get("相对路径", "").replace("/", "\\").casefold() not in moved_paths
    ]
    exact_counts: dict[str, int] = defaultdict(int)
    title_counts: dict[str, int] = defaultdict(int)
    for row in kept_rows:
        if row.get("精确重复组"):
            exact_counts[row["精确重复组"]] += 1
        if row.get("疑似同名组"):
            title_counts[row["疑似同名组"]] += 1
    for row in kept_rows:
        row["精确重复数"] = (
            str(exact_counts[row["精确重复组"]])
            if exact_counts.get(row["精确重复组"], 0) > 1
            else ""
        )
        row["疑似同名数"] = (
            str(title_counts[row["疑似同名组"]])
            if title_counts.get(row["疑似同名组"], 0) > 1
            else ""
        )

    temporary = args.catalog.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(kept_rows)
    temporary.replace(args.catalog)
    library_store.sync_catalog(
        args.catalog,
        args.output / "library.db",
        root,
    )

    library.generate_markdown_report(
        output,
        library.load_books_from_catalog(args.catalog, root),
        len({row["精确重复组"] for row in kept_rows if row.get("精确重复数")}),
        0,
        len({row["疑似同名组"] for row in kept_rows if row.get("疑似同名数")}),
    )
    library.generate_html(
        output,
        library.load_books_from_catalog(args.catalog, root),
    )
    log = {
        **summary,
        "moved": len(moved_paths),
        "failed": failed,
        "trash": str(trash_root),
    }
    (output / "exports" / "priority_dedupe_result.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(log, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
