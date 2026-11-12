#!/usr/bin/env python3
"""Local dynamic server with manual refresh for the ebook library."""

from __future__ import annotations

import argparse
import atexit
import csv
import hashlib
import json
import logging
import mimetypes
import os
import signal
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))
import build_library_index as library  # noqa: E402
import library_store  # noqa: E402
import library_paths  # noqa: E402
from recycle_bin import send_to_recycle_bin  # noqa: E402


BOOK_EXTENSIONS = {
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".djvu", ".uvz",
    ".cbz", ".cbr", ".chm", ".doc", ".docx", ".fb2", ".html", ".lit",
    ".odt", ".pdb", ".ppt", ".pptx", ".rtf", ".txt", ".xls", ".xlsx",
}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    ".book-library",
    "_书库整理",
}
IGNORED_FILENAMES = {
    ".ds_store", "desktop.ini", "thumbs.db", "requirements.txt",
    "license.txt", "readme.txt", "cmakelists.txt",
}


def path_key(value: str) -> str:
    return value.replace("/", "\\").casefold()


def filename_language(name: str) -> str:
    stem = Path(name).stem
    if any("\u3400" <= char <= "\u9fff" for char in stem):
        return "中文"
    if sum(char.isascii() and char.isalpha() for char in stem) >= 3:
        return "英文"
    return "无法判断"


def title_from_filename(name: str) -> str:
    title = Path(name).stem
    title = title.replace(" - 副本", "").replace(" - copy", "").strip()
    return title or name


def collection_fields(relative_path: str) -> tuple[str, str, str]:
    parts = Path(relative_path).parts
    collection = parts[0] if parts else ""
    if collection == "【00总文库】":
        category = parts[1] if len(parts) > 1 else collection
        subcategory = parts[2] if len(parts) > 2 else ""
    else:
        category = parts[1] if len(parts) > 1 else collection
        subcategory = parts[2] if len(parts) > 2 else ""
    return collection, category, subcategory


def scan_books(root: Path) -> dict[str, tuple[str, int, float]]:
    result: dict[str, tuple[str, int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name.casefold() not in SKIP_DIRS and not name.startswith("._")
        ]
        current = Path(dirpath)
        for name in filenames:
            if name.startswith("._") or name.casefold() in IGNORED_FILENAMES:
                continue
            path = current / name
            if path.suffix.casefold() not in BOOK_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = str(path.relative_to(root))
            result[path_key(relative)] = (relative, stat.st_size, stat.st_mtime)
    return result


def scan_fingerprint(scan: dict[str, tuple[str, int, float]]) -> str:
    digest = hashlib.sha1()
    for key in sorted(scan):
        relative, size, modified = scan[key]
        digest.update(
            f"{key}\0{relative}\0{size}\0{int(modified)}\n".encode("utf-8", "replace")
        )
    return digest.hexdigest()


class DynamicCatalog:
    def __init__(self, root: Path, output: Path, logger: logging.Logger) -> None:
        self.root = root
        self.output = output
        self.catalog_path = output / "exports" / "图书总目录.csv"
        self.html_path = output / "书库总览.html"
        self.logger = logger
        self.lock = threading.RLock()
        self.version = int(time.time() * 1000)
        self.last_scan = ""
        self.book_count = 0
        self.scanning = False
        self.last_fingerprint = ""
        self.ratings = self._load_ratings()

    def _load_ratings(self) -> dict[str, dict]:
        path = self.output / "cache" / "douban_ratings.json"
        if not path.exists():
            path = library_paths.LEGACY_DIR / "douban_ratings.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("ratings", {})
        except (OSError, json.JSONDecodeError):
            return {}

    def start(self) -> None:
        self.sync(force=True, reason="启动扫描")

    def _read_rows(self) -> tuple[list[dict], list[str]]:
        database = self.output / "library.db"
        if database.exists():
            rows = library_store.load_catalog_rows(database)
            return rows, list(rows[0].keys()) if rows else []
        with self.catalog_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            return list(reader), list(reader.fieldnames or [])

    def _new_row(
        self,
        relative_path: str,
        size: int,
        modified: float,
        template: dict,
    ) -> dict:
        name = Path(relative_path).name
        title = title_from_filename(name)
        language = filename_language(name)
        collection, category, subcategory = collection_fields(relative_path)
        shelf, smart_subcategory, tags = library.classify_book(title, "", relative_path)
        row = {field: "" for field in template}
        row.update(
            {
                "书名": title,
                "格式": Path(name).suffix.lstrip(".").upper(),
                "文件名语言": language,
                "语言": language if language != "无法判断" else "未知",
                "建议书架": shelf,
                "智能子类": smart_subcategory,
                "标签": tags,
                "现有总库": collection,
                "现有分类": category,
                "现有子类": subcategory,
                "大小MB": round(size / 1024 / 1024, 2),
                "元数据来源": "动态刷新（文件名推断）",
                "质量标记": "手动刷新新增；重建索引可补封面、评分和哈希",
                "修改时间": datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M"),
                "相对路径": relative_path,
            }
        )
        rating = self.ratings.get(library.normalize_title(title))
        if rating and rating.get("status") == "matched":
            row.update(
                {
                    "豆瓣评分": rating.get("rating") or "",
                    "豆瓣评分人数": rating.get("rating_count") or "",
                    "豆瓣匹配方式": rating.get("match_type", ""),
                    "豆瓣条目": rating.get("subject_url", ""),
                }
            )
        return row

    def _sync_rows(
        self,
        old_rows: list[dict],
        fields: list[str],
        scan: dict[str, tuple[str, int, float]],
    ) -> list[dict]:
        old_by_path = {path_key(row["相对路径"]): row for row in old_rows}
        scanned_keys = set(scan)
        missing_by_key: dict[str, list[dict]] = {}
        for row in old_rows:
            if path_key(row["相对路径"]) in scanned_keys:
                continue
            try:
                size_mb = float(row.get("大小MB") or 0)
            except ValueError:
                size_mb = 0
            key = f"{Path(row['相对路径']).name.casefold()}|{size_mb:.2f}"
            missing_by_key.setdefault(key, []).append(row)

        next_rows: list[dict] = []
        used: set[str] = set()
        for relative_key, (relative_path, size, modified) in sorted(scan.items()):
            size_mb = round(size / 1024 / 1024, 2)
            row = old_by_path.get(relative_key)
            if row is not None:
                row["大小MB"] = size_mb
                row["修改时间"] = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
            else:
                name = Path(relative_path).name
                key = f"{name.casefold()}|{size_mb:.2f}"
                candidates = [
                    candidate for candidate in missing_by_key.get(key, [])
                    if path_key(candidate["相对路径"]) not in used
                ]
                if len(candidates) == 1:
                    row = candidates[0]
                    row["相对路径"] = relative_path
                    row["大小MB"] = size_mb
                    row["修改时间"] = datetime.fromtimestamp(modified).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    collection, category, subcategory = collection_fields(relative_path)
                    row["现有总库"] = collection
                    row["现有分类"] = category
                    row["现有子类"] = subcategory
                    row["建议书架"], row["智能子类"], row["标签"] = library.classify_book(
                        row.get("书名", ""),
                        row.get("作者", ""),
                        relative_path,
                    )
                else:
                    row = self._new_row(
                        relative_path,
                        size,
                        modified,
                        old_rows[0] if old_rows else {field: "" for field in fields},
                    )
            row["相对路径"] = relative_path
            used.add(relative_key)
            next_rows.append(row)
        return next_rows

    def sync(self, force: bool, reason: str) -> int:
        scan = scan_books(self.root)
        fingerprint = scan_fingerprint(scan)
        with self.lock:
            if not force and fingerprint == self.last_fingerprint:
                return self.version
            self.scanning = True
            try:
                old_rows, fields = self._read_rows()
                if old_rows and not scan:
                    raise RuntimeError("扫描结果为空，拒绝覆盖现有目录")
                next_rows = self._sync_rows(old_rows, fields, scan)
                old_signature = sorted(
                    (row.get("相对路径", ""), row.get("大小MB", "")) for row in old_rows
                )
                next_signature = sorted(
                    (row.get("相对路径", ""), row.get("大小MB", "")) for row in next_rows
                )
                if force or old_signature != next_signature:
                    temporary = self.catalog_path.with_suffix(".csv.tmp")
                    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
                        writer = csv.DictWriter(stream, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(next_rows)
                    os.replace(temporary, self.catalog_path)
                    library_store.sync_catalog(
                        self.catalog_path,
                        self.output / "library.db",
                        self.root,
                    )
                    books = library.load_books_from_catalog(self.catalog_path, self.root)
                    library.generate_html(self.output, books)
                    self.book_count = len(books)
                    self.version = int(time.time() * 1000)
                self.last_fingerprint = fingerprint
                self.last_scan = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.logger.info("%s：%s 个文件，版本 %s", reason, len(next_rows), self.version)
                return self.version
            finally:
                self.scanning = False

    def status(self) -> dict:
        with self.lock:
            return {
                "ok": True,
                "service": "book-library",
                "version": self.version,
                "book_count": self.book_count,
                "last_scan": self.last_scan,
                "scanning": self.scanning,
                "root": str(self.root),
            }

    def html_bytes(self) -> bytes:
        with self.lock:
            return self.html_path.read_bytes()

    def recycle_books(self, relative_paths: list[str]) -> list[Path]:
        sources = []
        for relative_path in relative_paths:
            normalized = relative_path.replace("/", "\\").strip("\\")
            source = (self.root / normalized).resolve()
            if (
                not source.is_relative_to(self.root)
                or source.suffix.casefold() not in BOOK_EXTENSIONS
                or not source.is_file()
                or source.is_relative_to(self.output)
            ):
                raise ValueError(f"无效或不存在书籍文件：{relative_path}")
            sources.append(source)
        send_to_recycle_bin(sources)
        for source in sources:
            self.logger.info("移入 Windows 回收站：%s", source)
        return sources

    def open_in_default_app(self, relative_path: str) -> Path:
        normalized = relative_path.replace("/", "\\").strip("\\")
        source = (self.root / normalized).resolve()
        if (
            not source.is_relative_to(self.root)
            or source.suffix.casefold() not in BOOK_EXTENSIONS
            or not source.is_file()
            or source.is_relative_to(self.output)
        ):
            raise ValueError("无效或不存在书籍文件")
        if not hasattr(os, "startfile"):
            raise ValueError("当前系统不支持直接调用默认软件")
        os.startfile(str(source))
        self.logger.info("调用默认软件打开：%s", source)
        return source


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, catalog: DynamicCatalog):
        super().__init__(address, handler)
        self.catalog = catalog


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args) -> None:
        self.server.catalog.logger.info("%s - %s", self.address_string(), format % args)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, self.server.catalog.status())
            return
        if path == "/api/collections":
            items = library_store.list_collections(
                self.server.catalog.output / "library.db"
            )
            self._json(HTTPStatus.OK, {"ok": True, "items": items})
            return
        if path in {"/", "/书库总览.html"}:
            try:
                data = self.server.catalog.html_bytes()
            except OSError as error:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(error)})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/covers/"):
            relative = unquote(path.removeprefix("/covers/"))
            file_path = (self.server.catalog.output / "covers" / relative).resolve()
            covers_root = (self.server.catalog.output / "covers").resolve()
            if file_path.is_relative_to(covers_root) and file_path.is_file():
                data = file_path.read_bytes()
                content_type = mimetypes.guess_type(file_path.name)[0] or "image/jpeg"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON 无效"})
            return
        if path == "/api/rescan":
            version = self.server.catalog.sync(force=True, reason="手动刷新")
            self._json(HTTPStatus.OK, {"ok": True, "version": version})
            return
        if path in {"/api/delete", "/api/recycle"}:
            try:
                paths = payload.get("paths")
                if not isinstance(paths, list):
                    paths = [str(payload.get("path", ""))]
                sources = self.server.catalog.recycle_books(
                    [str(value) for value in paths]
                )
                version = self.server.catalog.sync(force=True, reason="删除后刷新")
            except (OSError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": version,
                    "paths": [
                        str(source.relative_to(self.server.catalog.root))
                        for source in sources
                    ],
                },
            )
            return
        if path == "/api/collections":
            try:
                item = library_store.create_collection(
                    self.server.catalog.output / "library.db",
                    str(payload.get("name", "")),
                )
            except (OSError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(HTTPStatus.OK, {"ok": True, "item": item})
            return
        if path == "/api/collections/members":
            try:
                result = library_store.add_collection_members(
                    self.server.catalog.output / "library.db",
                    int(payload.get("collection_id", 0)),
                    [str(value) for value in payload.get("paths", [])],
                )
            except (OSError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return
        if path == "/api/open":
            try:
                source = self.server.catalog.open_in_default_app(
                    str(payload.get("path", ""))
                )
            except (OSError, ValueError) as error:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                return
            self._json(
                HTTPStatus.OK,
                {"ok": True, "path": str(source)},
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def configure_logging(output: Path) -> logging.Logger:
    logger = logging.getLogger("book-library")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(output / "server.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=library_paths.ROOT)
    parser.add_argument("--output", type=Path, default=library_paths.DATA_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    if not output.is_relative_to(root):
        print(f"Output directory must be inside root: {output}", file=sys.stderr)
        return 1
    logger = configure_logging(output)
    pid_path = output / "server.pid"
    catalog = DynamicCatalog(root, output, logger)
    try:
        server = AppServer((args.host, args.port), Handler, catalog)
    except OSError:
        logger.exception("端口绑定失败：%s:%s", args.host, args.port)
        return 1
    catalog.start()
    pid_path.write_text(str(os.getpid()), encoding="ascii")

    def cleanup() -> None:
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(cleanup)

    def handle_signal(signum, frame) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    logger.info("服务启动：http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
