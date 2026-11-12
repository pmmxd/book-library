#!/usr/bin/env python3
"""Match the local catalog against a public Douban book snapshot."""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import library_paths


BRACKET_RE = re.compile(r"[\[(（【][^\[\]()（）【】]*[\])）】]")
COPY_RE = re.compile(r"(?i)\s*(?:[-_ ](?:副本|copy)\s*\d*|z-library|zlib)\s*$")
LEADING_RE = re.compile(r"^\s*(?:\d{2,4}[-_.、\s]+|《)")
TRAILING_RE = re.compile(r"[》\s]+$")
RATING_IN_NAME_RE = re.compile(r"豆瓣评分\s*(?:高达\s*)?([0-9]+(?:\.[0-9]+)?)")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def clean_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = COPY_RE.sub("", value)
    value = LEADING_RE.sub("", value)
    value = TRAILING_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def title_variants(title: str) -> list[str]:
    base = clean_title(title)
    variants: list[str] = []

    def add(value: str) -> None:
        value = clean_title(value)
        normalized = normalize(value)
        if len(normalized) >= 2 and normalized not in {normalize(item) for item in variants}:
            variants.append(value)

    add(base)

    no_brackets = BRACKET_RE.sub("", base)
    add(no_brackets)

    for separator in ("：", ":", "——", "—", " - "):
        if separator in base:
            add(base.split(separator, 1)[0])
            add(no_brackets.split(separator, 1)[0])

    compact = re.sub(r"\s+", "", no_brackets)
    for marker in ("套装", "全集", "典藏版", "纪念版", "修订版", "新版"):
        position = compact.rfind(marker)
        if position >= 2 and position >= len(compact) - 10:
            add(compact[:position])
            break
    return variants


def best_title_map(dataset: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for subject_id, title, rating, rating_count in dataset["books"]:
        normalized = normalize(title)
        if not normalized:
            continue
        rating_count = rating_count or 0
        candidate = {
            "subject_id": str(subject_id),
            "title": title,
            "rating": rating,
            "rating_count": rating_count,
        }
        existing = result.get(normalized)
        if existing is None or rating_count > existing["rating_count"]:
            result[normalized] = candidate
    return result


def match_book(title: str, title_map: dict[str, dict]) -> tuple[dict | None, str]:
    for index, variant in enumerate(title_variants(title)):
        matched = title_map.get(normalize(variant))
        if matched and matched["rating"] is not None:
            match_type = "完整书名精确匹配" if index == 0 else "去除副标题/版本说明后匹配"
            return matched, match_type
    return None, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=library_paths.EXPORTS_DIR / "图书总目录.csv",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=library_paths.LEGACY_DIR / "douban_all_books.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.CACHE_DIR / "douban_ratings.json",
    )
    args = parser.parse_args()

    with args.dataset.open(encoding="utf-8") as stream:
        dataset = json.load(stream)
    title_map = best_title_map(dataset)

    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    cache: dict[str, dict] = {}
    for row in rows:
        title = row.get("书名", "")
        key = normalize(title)
        if not key or key in cache:
            continue
        matched, match_type = match_book(title, title_map)
        filename_rating = RATING_IN_NAME_RE.search(row.get("相对路径", ""))
        if filename_rating:
            cache[key] = {
                "status": "matched",
                "rating": float(filename_rating.group(1)),
                "rating_count": None,
                "subject_id": "",
                "subject_url": "",
                "matched_title": title,
                "match_type": "文件名标注",
            }
        elif matched:
            cache[key] = {
                "status": "matched",
                "rating": matched["rating"],
                "rating_count": matched["rating_count"],
                "subject_id": matched["subject_id"],
                "subject_url": f"https://book.douban.com/subject/{matched['subject_id']}/",
                "matched_title": matched["title"],
                "match_type": match_type,
            }
        else:
            cache[key] = {
                "status": "unmatched",
                "rating": None,
                "rating_count": None,
                "subject_id": "",
                "subject_url": "",
                "matched_title": "",
                "match_type": "",
            }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "yuzhounh/douban-books-ranking data snapshot 2026-08-11",
        "source_url": "https://github.com/yuzhounh/douban-books-ranking",
        "source_disclaimer": "豆瓣图书元数据与评分归豆瓣及原权利人所有，仅供个人离线索引使用。",
        "dataset_count": dataset.get("count", len(dataset.get("books", []))),
        "ratings": cache,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    matched = sum(1 for item in cache.values() if item["status"] == "matched")
    row_match = sum(
        1 for row in rows if cache.get(normalize(row.get("书名", "")), {}).get("status") == "matched"
    )
    print(
        json.dumps(
            {
                "unique_titles": len(cache),
                "matched_unique_titles": matched,
                "matched_rows": row_match,
                "matched_row_pct": round(row_match / len(rows) * 100, 1),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
