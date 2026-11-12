#!/usr/bin/env python3
"""Minimal local bridge for opening books and generating AI summaries."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import library_store
import library_paths
from recycle_bin import send_to_recycle_bin


BOOK_EXTENSIONS = {
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".djvu", ".uvz",
    ".cbz", ".cbr", ".chm", ".doc", ".docx", ".fb2", ".html", ".lit",
    ".odt", ".pdb", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx",
}


class BridgeState:
    def __init__(self, root: Path, output: Path, model: str) -> None:
        self.root = root
        self.output = output
        self.model = model
        self.db_path = output / "library.db"
        self.cache_path = output / "cache" / "ai_summaries.json"
        self.lock = threading.RLock()
        self.cache = self._load_cache()
        self.collection_cache = self._load_collections()

    def _load_cache(self) -> dict[str, dict]:
        if self.db_path.exists():
            try:
                return library_store.load_ai_summaries(self.db_path)
            except Exception:
                return {}
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)

    def _load_collections(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        immutable = not wal_path.exists() or wal_path.stat().st_size == 0
        try:
            return library_store.list_collections(self.db_path, immutable)
        except Exception:
            return []

    def validate_book(self, relative_path: str) -> Path:
        normalized = relative_path.replace("/", "\\").strip("\\")
        source = (self.root / normalized).resolve()
        if (
            not source.is_relative_to(self.root)
            or source.suffix.casefold() not in BOOK_EXTENSIONS
            or not source.is_file()
            or source.is_relative_to(self.output)
        ):
            raise ValueError("无效或不存在书籍文件")
        return source

    def open_book(self, relative_path: str) -> str:
        source = self.validate_book(relative_path)
        if not hasattr(os, "startfile"):
            raise ValueError("当前系统不支持调用默认软件")
        os.startfile(str(source))
        return str(source)

    def reveal_book(self, relative_path: str) -> str:
        source = self.validate_book(relative_path)
        subprocess.Popen(
            ["explorer.exe", f"/select,{source}"],
            close_fds=True,
        )
        return str(source)

    def collections(self) -> list[dict]:
        with self.lock:
            return [
                {**item, "paths": list(item["paths"])}
                for item in self.collection_cache
            ]

    def create_collection(self, name: str) -> dict:
        with self.lock:
            item = library_store.create_collection(self.db_path, name)
            self.collection_cache.append(item)
            return {**item, "paths": list(item["paths"])}

    def add_collection_members(
        self, collection_id: int, relative_paths: list[str]
    ) -> dict:
        with self.lock:
            result = library_store.add_collection_members(
                self.db_path, collection_id, relative_paths
            )
            collection = next(
                (item for item in self.collection_cache if item["id"] == collection_id),
                None,
            )
            if collection is not None:
                existing = {path.casefold() for path in collection["paths"]}
                for path in relative_paths:
                    normalized = str(path).replace("/", "\\").strip("\\")
                    if normalized and normalized.casefold() not in existing:
                        collection["paths"].append(normalized)
                        existing.add(normalized.casefold())
            return result

    def recycle_books(self, relative_paths: list[str]) -> list[str]:
        sources = [self.validate_book(path) for path in relative_paths]
        send_to_recycle_bin(sources)
        normalized = [str(source.relative_to(self.root)) for source in sources]
        library_store.forget_media_paths(self.db_path, normalized)
        removed = {path.casefold() for path in normalized}
        with self.lock:
            for collection in self.collection_cache:
                collection["paths"] = [
                    path for path in collection["paths"]
                    if path.casefold() not in removed
                ]
        return normalized

    def summaries(self) -> dict[str, dict]:
        with self.lock:
            return dict(self.cache)

    def status(self) -> dict:
        progress_path = self.output / "runtime" / "ai_batch_progress.json"
        batch = None
        if progress_path.exists():
            try:
                batch = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                batch = None
        return {
            "ok": True,
            "service": "book-bridge",
            "model": self.model,
            "cache_count": len(self.summaries()),
            "batch": batch,
        }

    def summary(self, payload: dict) -> dict:
        relative_path = str(payload.get("path", ""))
        source = self.validate_book(relative_path)
        if source.suffix.casefold() == ".pdf":
            raise ValueError("PDF 书籍不生成 AI 简介")
        key = relative_path.replace("/", "\\").casefold()
        with self.lock:
            cached = self.cache.get(key)
        if cached:
            return cached

        book = {
            "title": str(payload.get("title", "")),
            "author": str(payload.get("author", "")),
            "category": str(payload.get("category", "")),
            "tags": payload.get("tags", []),
        }
        prompt = (
            "/no_think 你是中文图书编辑。根据书名、作者、分类和标签，为这本书生成"
            "简介和推荐理由。简介35-60字，推荐理由25-45字。信息不足时只能合理概括，"
            "不得编造具体情节、人物或事实。只输出JSON："
            '{"summary":"...","reason":"..."}\n'
            + json.dumps(book, ensure_ascii=False)
        )
        response = requests.post(
            "http://127.0.0.1:11434/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 260},
            },
            timeout=180,
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "{}")
        generated = json.loads(content)
        result = {
            "summary": str(generated.get("summary", "")).strip(),
            "reason": str(generated.get("reason", "")).strip(),
            "model": self.model,
            "generated_at": int(time.time()),
        }
        if not result["summary"] or not result["reason"]:
            raise ValueError("AI 返回内容不完整")
        with self.lock:
            if self.db_path.exists():
                library_store.upsert_ai_summary(
                    self.db_path,
                    relative_path,
                    self.model,
                    result["summary"],
                    result["reason"],
                )
                self.cache[key] = result
            else:
                self.cache[key] = result
                self._save_cache()
        return result


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, state: BridgeState):
        super().__init__(address, handler)
        self.state = state


class Handler(BaseHTTPRequestHandler):
    server: BridgeServer

    def log_message(self, format: str, *args) -> None:
        pass

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self._send(HTTPStatus.OK, self.server.state.status())
            return
        if self.path == "/api/ai-summaries":
            self._send(HTTPStatus.OK, {"ok": True, "items": self.server.state.summaries()})
            return
        if self.path == "/api/collections":
            self._send(
                HTTPStatus.OK,
                {"ok": True, "items": self.server.state.collections()},
            )
            return
        self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON 无效"})
            return
        try:
            if self.path == "/api/open":
                path = self.server.state.open_book(str(payload.get("path", "")))
                self._send(HTTPStatus.OK, {"ok": True, "path": path})
                return
            if self.path == "/api/reveal":
                path = self.server.state.reveal_book(str(payload.get("path", "")))
                self._send(HTTPStatus.OK, {"ok": True, "path": path})
                return
            if self.path == "/api/recycle":
                paths = self.server.state.recycle_books(
                    [str(value) for value in payload.get("paths", [])]
                )
                self._send(HTTPStatus.OK, {"ok": True, "paths": paths})
                return
            if self.path == "/api/collections":
                item = self.server.state.create_collection(
                    str(payload.get("name", ""))
                )
                self._send(HTTPStatus.OK, {"ok": True, "item": item})
                return
            if self.path == "/api/collections/members":
                result = self.server.state.add_collection_members(
                    int(payload.get("collection_id", 0)),
                    [str(value) for value in payload.get("paths", [])],
                )
                self._send(HTTPStatus.OK, {"ok": True, **result})
                return
            if self.path == "/api/ai-summary":
                result = self.server.state.summary(payload)
                self._send(HTTPStatus.OK, {"ok": True, **result})
                return
        except Exception as error:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            return
        self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=library_paths.ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.DATA_DIR,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--model", default="qwen3.5:4b")
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    if not output.is_relative_to(root):
        raise SystemExit("Output directory must be inside library root")
    pid_path = output / "runtime" / "bridge.pid"
    state = BridgeState(root, output, args.model)
    server = BridgeServer((args.host, args.port), Handler, state)
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        pid_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
