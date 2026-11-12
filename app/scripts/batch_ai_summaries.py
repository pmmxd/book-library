#!/usr/bin/env python3
"""Generate AI summaries for all non-PDF books through the local bridge."""

from __future__ import annotations

import argparse
import csv
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import library_paths


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=library_paths.EXPORTS_DIR / "图书总目录.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.DATA_DIR,
    )
    parser.add_argument("--bridge", default="http://127.0.0.1:18767")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    progress_path = args.output / "runtime" / "ai_batch_progress.json"
    log_path = args.output / "runtime" / "ai_batch.log"
    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        books = [
            row
            for row in csv.DictReader(stream)
            if row.get("格式", "").upper() != "PDF"
        ]
    books.sort(
        key=lambda row: (
            row.get("建议书架", ""),
            row.get("书名", "").casefold(),
            row.get("相对路径", "").casefold(),
        )
    )
    existing = requests.get(
        f"{args.bridge}/api/ai-summaries", timeout=10
    ).json().get("items", {})
    key = lambda row: row["相对路径"].replace("/", "\\").casefold()
    todo = [row for row in books if key(row) not in existing]
    if args.limit > 0:
        todo = todo[: args.limit]
    total = len(todo)
    completed = 0
    errors = 0
    lock = threading.Lock()
    started_at = datetime.now().isoformat(timespec="seconds")

    def progress(running: bool) -> None:
        write_json(
            progress_path,
            {
                "running": running,
                "total": total,
                "completed": completed,
                "errors": errors,
                "remaining": max(0, total - completed - errors),
                "started_at": started_at,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    progress(True)
    log = log_path.open("a", encoding="utf-8")
    log.write(f"\n{started_at} start: total={total}, workers={args.workers}\n")
    log.flush()

    def generate(row: dict) -> tuple[str, bool, str]:
        payload = {
            "path": row["相对路径"],
            "title": row.get("书名", ""),
            "author": row.get("作者", ""),
            "category": row.get("建议书架", ""),
            "tags": [tag for tag in row.get("标签", "").split("；") if tag],
        }
        response = requests.post(
            f"{args.bridge}/api/ai-summary",
            json=payload,
            timeout=180,
        )
        if not response.ok:
            try:
                message = response.json().get("error", response.text)
            except Exception:
                message = response.text
            return row["相对路径"], False, message
        result = response.json()
        if not result.get("summary") or not result.get("reason"):
            return row["相对路径"], False, "返回内容不完整"
        return row["相对路径"], True, ""

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(generate, row): row for row in todo}
        for future in as_completed(futures):
            path, ok, message = future.result()
            with lock:
                if ok:
                    completed += 1
                else:
                    errors += 1
                    log.write(f"{datetime.now().isoformat()} ERROR {path}: {message}\n")
                    log.flush()
                count = completed + errors
                if count % 10 == 0 or count == total:
                    progress(True)
                    log.write(
                        f"{datetime.now().isoformat()} progress "
                        f"{count}/{total} ok={completed} errors={errors}\n"
                    )
                    log.flush()
                    print(f"{count}/{total} ok={completed} errors={errors}", flush=True)

    log.close()
    progress(False)
    print(f"Completed: {completed}, errors: {errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
