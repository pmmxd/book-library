#!/usr/bin/env python3
"""SQLite storage for the book library."""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = "3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_schema(connection)
    return connection


def connect_readonly(path: Path, immutable: bool = False) -> sqlite3.Connection:
    options = "mode=ro&immutable=1" if immutable else "mode=ro"
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?{options}",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            format TEXT NOT NULL,
            filename_language TEXT,
            language TEXT,
            title TEXT NOT NULL,
            author TEXT,
            content_hash TEXT,
            size_bytes INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            cover_path TEXT,
            shelf TEXT,
            smart_subcategory TEXT,
            collection TEXT,
            category TEXT,
            subcategory TEXT,
            metadata_source TEXT,
            quality_flags TEXT,
            page_count INTEGER,
            isbn TEXT,
            publisher TEXT,
            published_at TEXT,
            work_key TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS douban_metadata (
            media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
            subject_id TEXT,
            rating REAL,
            rating_count INTEGER,
            matched_title TEXT,
            match_type TEXT,
            subject_url TEXT,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ai_metadata (
            media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
            model TEXT,
            summary TEXT NOT NULL,
            reason TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_metadata (
            media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
            rating INTEGER,
            featured INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            reading_status TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS media_tags (
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            source TEXT NOT NULL DEFAULT 'auto',
            confidence REAL NOT NULL DEFAULT 1,
            PRIMARY KEY (media_id, tag_id, source)
        );
        CREATE TABLE IF NOT EXISTS duplicate_groups (
            id INTEGER PRIMARY KEY,
            external_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 1,
            keeper_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS duplicate_members (
            group_id INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            role TEXT,
            reason TEXT,
            PRIMARY KEY (group_id, media_id)
        );
        CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS collection_members (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (collection_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_media_format ON media(format);
        CREATE INDEX IF NOT EXISTS idx_media_shelf ON media(shelf);
        CREATE INDEX IF NOT EXISTS idx_media_author ON media(author COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_media_hash ON media(content_hash);
        CREATE INDEX IF NOT EXISTS idx_media_modified ON media(modified_ns DESC);
        CREATE INDEX IF NOT EXISTS idx_media_work_key ON media(work_key);
        CREATE INDEX IF NOT EXISTS idx_douban_rating ON douban_metadata(rating DESC);
        CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag_id, media_id);
        CREATE INDEX IF NOT EXISTS idx_user_rating ON user_metadata(rating DESC);
        CREATE INDEX IF NOT EXISTS idx_collection_members_media
            ON collection_members(media_id, collection_id);
        """
    )
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(media)")
    }
    if "smart_subcategory" not in columns:
        connection.execute("ALTER TABLE media ADD COLUMN smart_subcategory TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_smart_subcategory "
        "ON media(smart_subcategory)"
    )
    current = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if current is None or current["value"] != SCHEMA_VERSION:
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
    connection.commit()


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _work_key(row: dict) -> str:
    title = _normalize(row.get("书名", ""))
    author = _normalize(row.get("作者", ""))
    return f"{title}|{author}" if title else ""


def sync_catalog(csv_path: Path, db_path: Path, root: Path) -> dict[str, int]:
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    connection = connect(db_path)
    try:
        with connection:
            existing_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT id,relative_path,work_key,size_bytes,content_hash FROM media"
                )
            ]
            existing_paths = {row["relative_path"] for row in existing_rows}
            media_ids: dict[str, int] = {}
            for row in rows:
                relative = row["相对路径"].replace("/", "\\")
                source = root / relative
                stat = source.stat()
                values = {
                    "relative_path": relative,
                    "format": row.get("格式", "").upper(),
                    "filename_language": row.get("文件名语言", ""),
                    "language": row.get("语言", ""),
                    "title": row.get("书名", ""),
                    "author": row.get("作者", ""),
                    "size_bytes": stat.st_size,
                    "modified_ns": stat.st_mtime_ns,
                    "cover_path": row.get("封面", ""),
                    "shelf": row.get("建议书架", ""),
                    "smart_subcategory": row.get("智能子类", ""),
                    "collection": row.get("现有总库", ""),
                    "category": row.get("现有分类", ""),
                    "subcategory": row.get("现有子类", ""),
                    "metadata_source": row.get("元数据来源", ""),
                    "quality_flags": row.get("质量标记", ""),
                    "page_count": int(row["页数"]) if row.get("页数", "").isdigit() else None,
                    "isbn": row.get("ISBN", ""),
                    "publisher": row.get("出版社", ""),
                    "published_at": row.get("出版时间", ""),
                    "work_key": _work_key(row),
                }
                connection.execute(
                    """
                    INSERT INTO media (
                        relative_path, format, filename_language, language, title, author,
                        size_bytes, modified_ns, cover_path, shelf, smart_subcategory, collection, category,
                        subcategory, metadata_source, quality_flags, page_count, isbn,
                        publisher, published_at, work_key
                    ) VALUES (
                        :relative_path, :format, :filename_language, :language, :title, :author,
                        :size_bytes, :modified_ns, :cover_path, :shelf, :smart_subcategory, :collection, :category,
                        :subcategory, :metadata_source, :quality_flags, :page_count, :isbn,
                        :publisher, :published_at, :work_key
                    )
                    ON CONFLICT(relative_path) DO UPDATE SET
                        format=excluded.format,
                        filename_language=excluded.filename_language,
                        language=excluded.language,
                        title=excluded.title,
                        author=excluded.author,
                        size_bytes=excluded.size_bytes,
                        modified_ns=excluded.modified_ns,
                        cover_path=excluded.cover_path,
                        shelf=excluded.shelf,
                        smart_subcategory=excluded.smart_subcategory,
                        collection=excluded.collection,
                        category=excluded.category,
                        subcategory=excluded.subcategory,
                        metadata_source=excluded.metadata_source,
                        quality_flags=excluded.quality_flags,
                        page_count=excluded.page_count,
                        isbn=excluded.isbn,
                        publisher=excluded.publisher,
                        published_at=excluded.published_at,
                        work_key=excluded.work_key,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    values,
                )
                media_id = connection.execute(
                    "SELECT id FROM media WHERE relative_path=?", (relative,)
                ).fetchone()[0]
                media_ids[relative.casefold()] = media_id

                rating = row.get("豆瓣评分", "")
                if rating:
                    connection.execute(
                        """
                        INSERT INTO douban_metadata (
                            media_id, subject_id, rating, rating_count, matched_title,
                            match_type, subject_url, fetched_at
                        ) VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                        ON CONFLICT(media_id) DO UPDATE SET
                            subject_id=excluded.subject_id,
                            rating=excluded.rating,
                            rating_count=excluded.rating_count,
                            matched_title=excluded.matched_title,
                            match_type=excluded.match_type,
                            subject_url=excluded.subject_url,
                            fetched_at=CURRENT_TIMESTAMP
                        """,
                        (
                            media_id,
                            row.get("豆瓣条目", "").rstrip("/").split("/")[-1],
                            float(rating),
                            int(row["豆瓣评分人数"]) if row.get("豆瓣评分人数", "").isdigit() else None,
                            row.get("书名", ""),
                            row.get("豆瓣匹配方式", ""),
                            row.get("豆瓣条目", ""),
                        ),
                    )

            current_paths = {row["相对路径"].replace("/", "\\") for row in rows}
            new_paths = current_paths - existing_paths
            relinked: set[int] = set()
            new_rows: dict[tuple[str, int], list[int]] = defaultdict(list)
            for row in connection.execute("SELECT id,work_key,size_bytes FROM media"):
                if row["work_key"]:
                    new_rows[(row["work_key"], row["size_bytes"])].append(row["id"])
            for old in existing_rows:
                if old["relative_path"] in current_paths:
                    continue
                candidates = new_rows.get((old["work_key"], old["size_bytes"]), [])
                if len(candidates) != 1:
                    continue
                candidate_id = candidates[0]
                if candidate_id in relinked:
                    continue
                candidate_path = connection.execute(
                    "SELECT relative_path FROM media WHERE id=?", (candidate_id,)
                ).fetchone()
                if not candidate_path or candidate_path["relative_path"] not in new_paths:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ai_metadata(media_id,model,summary,reason,generated_at)
                    SELECT ?,model,summary,reason,generated_at FROM ai_metadata WHERE media_id=?
                    """,
                    (candidate_id, old["id"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO user_metadata(media_id,rating,featured,note,reading_status,updated_at)
                    SELECT ?,rating,featured,note,reading_status,updated_at
                    FROM user_metadata WHERE media_id=?
                    """,
                    (candidate_id, old["id"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO douban_metadata(
                        media_id,subject_id,rating,rating_count,matched_title,match_type,subject_url,fetched_at
                    )
                    SELECT ?,subject_id,rating,rating_count,matched_title,match_type,subject_url,fetched_at
                    FROM douban_metadata WHERE media_id=?
                    """,
                    (candidate_id, old["id"]),
                )
                connection.execute(
                    "UPDATE media SET content_hash=COALESCE(content_hash,?) WHERE id=?",
                    (old["content_hash"], candidate_id),
                )
                connection.execute("DELETE FROM media WHERE id=?", (old["id"],))
                relinked.add(candidate_id)

            for missing in existing_paths - current_paths:
                connection.execute("DELETE FROM media WHERE relative_path=?", (missing,))

            connection.execute("DELETE FROM media_tags WHERE source='auto'")
            tag_names = Counter(
                tag
                for row in rows
                for tag in row.get("标签", "").split("；")
                if tag
            )
            for tag in tag_names:
                connection.execute(
                    "INSERT INTO tags(name) VALUES(?) ON CONFLICT(name) DO NOTHING",
                    (tag,),
                )
            tag_ids = {row["name"]: row["id"] for row in connection.execute("SELECT id,name FROM tags")}
            for row in rows:
                media_id = media_ids[row["相对路径"].replace("/", "\\").casefold()]
                for tag in row.get("标签", "").split("；"):
                    if tag:
                        connection.execute(
                            "INSERT OR IGNORE INTO media_tags(media_id,tag_id,source) VALUES(?,?,'auto')",
                            (media_id, tag_ids[tag]),
                        )

            connection.execute("DELETE FROM duplicate_groups WHERE kind IN ('exact_hash','title_similarity')")
            grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for row in rows:
                if row.get("精确重复组"):
                    grouped[("exact_hash", row["精确重复组"])].append(row)
                if row.get("疑似同名组"):
                    grouped[("title_similarity", row["疑似同名组"])].append(row)
            for (kind, external_key), members in grouped.items():
                cursor = connection.execute(
                    "INSERT INTO duplicate_groups(external_key,kind,confidence) VALUES(?,?,1)",
                    (f"{kind}:{external_key}", kind),
                )
                group_id = cursor.lastrowid
                for member in members:
                    media_id = media_ids[member["相对路径"].replace("/", "\\").casefold()]
                    connection.execute(
                        "INSERT INTO duplicate_members(group_id,media_id,role) VALUES(?,?,?)",
                        (group_id, media_id, "candidate"),
                    )
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("media", "tags", "media_tags", "douban_metadata", "duplicate_groups")
        }
        return counts
    finally:
        connection.close()


def import_ai_summaries(db_path: Path, json_path: Path) -> int:
    if not json_path.exists():
        return 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    items = payload.get("items", payload)
    connection = connect(db_path)
    imported = 0
    try:
        with connection:
            media_rows = {
                row["relative_path"].casefold(): row["id"]
                for row in connection.execute("SELECT id,relative_path FROM media")
            }
            for path, item in items.items():
                media_id = media_rows.get(path.replace("/", "\\").casefold())
                if not media_id or not item.get("summary") or not item.get("reason"):
                    continue
                generated = item.get("generated_at")
                if isinstance(generated, (int, float)):
                    generated = datetime.fromtimestamp(generated).isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO ai_metadata(media_id,model,summary,reason,generated_at)
                    VALUES(?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))
                    ON CONFLICT(media_id) DO UPDATE SET
                        model=excluded.model,
                        summary=excluded.summary,
                        reason=excluded.reason,
                        generated_at=excluded.generated_at
                    """,
                    (
                        media_id,
                        item.get("model", ""),
                        item["summary"],
                        item["reason"],
                        generated,
                    ),
                )
                imported += 1
        return imported
    finally:
        connection.close()


def load_ai_summaries(db_path: Path) -> dict[str, dict]:
    connection = connect(db_path)
    try:
        return {
            row["relative_path"].replace("/", "\\").casefold(): {
                "summary": row["summary"],
                "reason": row["reason"],
                "model": row["model"],
                "generated_at": row["generated_at"],
            }
            for row in connection.execute(
                """
                SELECT m.relative_path,a.summary,a.reason,a.model,a.generated_at
                FROM ai_metadata a JOIN media m ON m.id=a.media_id
                """
            )
        }
    finally:
        connection.close()


def upsert_ai_summary(
    db_path: Path,
    relative_path: str,
    model: str,
    summary: str,
    reason: str,
) -> None:
    connection = connect(db_path)
    try:
        with connection:
            media = connection.execute(
                "SELECT id FROM media WHERE relative_path=?",
                (relative_path.replace("/", "\\"),),
            ).fetchone()
            if not media:
                raise ValueError("书籍尚未写入媒体数据库")
            connection.execute(
                """
                INSERT INTO ai_metadata(media_id,model,summary,reason,generated_at)
                VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(media_id) DO UPDATE SET
                    model=excluded.model,
                    summary=excluded.summary,
                    reason=excluded.reason,
                    generated_at=CURRENT_TIMESTAMP
                """,
                (media["id"], model, summary, reason),
            )
    finally:
        connection.close()


def load_catalog_rows(db_path: Path) -> list[dict]:
    connection = connect(db_path)
    try:
        rows = connection.execute(
            """
            SELECT
                m.*,
                d.rating AS douban_rating,
                d.rating_count AS douban_rating_count,
                d.subject_url AS douban_subject_url,
                d.match_type AS douban_match_type,
                COALESCE(GROUP_CONCAT(t.name, '；'), '') AS tags,
                exact.external_key AS exact_group,
                title.external_key AS title_group,
                (SELECT COUNT(*) FROM duplicate_members x WHERE x.group_id=exact.id) AS exact_count,
                (SELECT COUNT(*) FROM duplicate_members x WHERE x.group_id=title.id) AS title_count
            FROM media m
            LEFT JOIN douban_metadata d ON d.media_id=m.id
            LEFT JOIN media_tags mt ON mt.media_id=m.id AND mt.source='auto'
            LEFT JOIN tags t ON t.id=mt.tag_id
            LEFT JOIN duplicate_groups exact ON exact.kind='exact_hash'
                AND exact.id=(SELECT group_id FROM duplicate_members WHERE media_id=m.id
                              AND group_id IN (SELECT id FROM duplicate_groups WHERE kind='exact_hash') LIMIT 1)
            LEFT JOIN duplicate_groups title ON title.kind='title_similarity'
                AND title.id=(SELECT group_id FROM duplicate_members WHERE media_id=m.id
                              AND group_id IN (SELECT id FROM duplicate_groups WHERE kind='title_similarity') LIMIT 1)
            GROUP BY m.id
            ORDER BY m.shelf, m.title COLLATE NOCASE, m.relative_path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        connection.close()
    result = []
    for row in rows:
        modified = datetime.fromtimestamp(row["modified_ns"] / 1_000_000_000)
        result.append(
            {
                "书名": row["title"],
                "作者": row["author"] or "",
                "格式": row["format"],
                "文件名语言": row["filename_language"] or "",
                "语言": row["language"] or "",
                "豆瓣评分": row["douban_rating"] if row["douban_rating"] is not None else "",
                "豆瓣评分人数": row["douban_rating_count"] or "",
                "豆瓣匹配方式": row["douban_match_type"] or "",
                "豆瓣条目": row["douban_subject_url"] or "",
                "封面": row["cover_path"] or "",
                "标签": row["tags"] or "",
                "建议书架": row["shelf"] or "",
                "智能子类": row["smart_subcategory"] or "",
                "现有总库": row["collection"] or "",
                "现有分类": row["category"] or "",
                "现有子类": row["subcategory"] or "",
                "大小MB": round(row["size_bytes"] / 1024 / 1024, 2),
                "页数": row["page_count"] or "",
                "ISBN": row["isbn"] or "",
                "出版社": row["publisher"] or "",
                "出版时间": row["published_at"] or "",
                "元数据来源": row["metadata_source"] or "",
                "质量标记": row["quality_flags"] or "",
                "精确重复组": row["exact_group"] or "",
                "精确重复数": row["exact_count"] if row["exact_count"] > 1 else "",
                "疑似同名组": row["title_group"] or "",
                "疑似同名数": row["title_count"] if row["title_count"] > 1 else "",
                "修改时间": modified.strftime("%Y-%m-%d %H:%M"),
                "相对路径": row["relative_path"],
            }
        )
    return result


def list_collections(db_path: Path, immutable: bool = False) -> list[dict]:
    connection = connect_readonly(db_path, immutable)
    try:
        collections = connection.execute(
            "SELECT id,name,created_at,updated_at FROM collections ORDER BY name COLLATE NOCASE"
        ).fetchall()
        members = connection.execute(
            "SELECT cm.collection_id,m.relative_path "
            "FROM collection_members cm JOIN media m ON m.id=cm.media_id "
            "ORDER BY m.relative_path COLLATE NOCASE"
        ).fetchall()
    finally:
        connection.close()
    paths_by_collection: dict[int, list[str]] = defaultdict(list)
    for member in members:
        paths_by_collection[member["collection_id"]].append(member["relative_path"])
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "paths": paths_by_collection[row["id"]],
        }
        for row in collections
    ]

def create_collection(db_path: Path, name: str) -> dict:
    name = name.strip()
    if not name or len(name) > 80:
        raise ValueError("书集名称需为 1-80 个字符")
    connection = connect(db_path)
    try:
        try:
            cursor = connection.execute("INSERT INTO collections(name) VALUES(?)", (name,))
            connection.commit()
        except sqlite3.IntegrityError as error:
            raise ValueError("书集名称已存在") from error
        row = connection.execute(
            "SELECT id,name,created_at,updated_at FROM collections WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    finally:
        connection.close()
    return {**dict(row), "paths": []}

def add_collection_members(
    db_path: Path, collection_id: int, relative_paths: list[str]
) -> dict:
    normalized = list({
        str(path).replace("/", "\\").strip("\\")
        for path in relative_paths
        if str(path).strip()
    })
    if not normalized:
        raise ValueError("未选择书籍")
    connection = connect(db_path)
    try:
        collection = connection.execute(
            "SELECT id FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        if not collection:
            raise ValueError("书集不存在")
        media_by_path: dict[str, int] = {}
        for offset in range(0, len(normalized), 400):
            chunk = normalized[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT id,relative_path FROM media "
                f"WHERE relative_path COLLATE NOCASE IN ({placeholders})",
                chunk,
            ).fetchall()
            media_by_path.update({row["relative_path"]: row["id"] for row in rows})
        cursor = connection.executemany(
            "INSERT OR IGNORE INTO collection_members(collection_id,media_id) VALUES(?,?)",
            [(collection_id, media_id) for media_id in media_by_path.values()],
        )
        connection.execute(
            "UPDATE collections SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (collection_id,),
        )
        connection.commit()
        added = cursor.rowcount
    finally:
        connection.close()
    return {
        "added": added,
        "missing": len(normalized) - len(media_by_path),
    }

def forget_media_paths(db_path: Path, relative_paths: list[str]) -> int:
    normalized = [
        str(path).replace("/", "\\").strip("\\")
        for path in relative_paths
        if str(path).strip()
    ]
    if not normalized:
        return 0
    connection = connect(db_path)
    try:
        removed = 0
        connection.execute("BEGIN IMMEDIATE")
        for offset in range(0, len(normalized), 400):
            chunk = normalized[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = connection.execute(
                f"DELETE FROM media WHERE relative_path COLLATE NOCASE IN ({placeholders})",
                chunk,
            )
            removed += cursor.rowcount
        connection.commit()
    finally:
        connection.close()
    return removed


def export_catalog_csv(db_path: Path, output: Path) -> int:
    rows = load_catalog_rows(db_path)
    fields = list(rows[0].keys()) if rows else []
    temporary = output.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, output)
    return len(rows)
