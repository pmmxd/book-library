#!/usr/bin/env python3
"""Extract compact, local EPUB cover thumbnails for the offline catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageOps
import library_paths


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
THUMB_SIZE = (180, 250)


def text_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[\W_]+", "", value)


def resolve_archive_path(base: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), href))


def unwrap_cover_reference(data: bytes, base: str, archive: zipfile.ZipFile) -> bytes:
    for _ in range(2):
        if not data.lstrip().startswith((b"<html", b"<svg", b"<?xml")):
            break
        text = data.decode("utf-8", errors="replace")
        match = re.search(
            r"""(?is)(?:src|href|xlink:href)\s*=\s*["']([^"']+)["']""",
            text,
        )
        if not match:
            break
        target = match.group(1).strip()
        if not target or target.startswith(("data:", "http:", "https:")):
            break
        base = resolve_archive_path(base, target)
        try:
            data = archive.read(base)
        except (KeyError, OSError, zipfile.BadZipFile):
            break
    return data


def epub_cover_bytes(path: Path) -> bytes | None:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        return None

    try:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                (
                    item.attrib.get("full-path", "")
                    for item in container.iter()
                    if item.tag.rsplit("}", 1)[-1] == "rootfile"
                ),
                "",
            )
            if not rootfile:
                return None
            opf = ET.fromstring(archive.read(rootfile))
        except (KeyError, ET.ParseError, zipfile.BadZipFile):
            return None

        manifest: dict[str, dict[str, str]] = {}
        for item in opf.iter():
            if item.tag.rsplit("}", 1)[-1] != "item":
                continue
            item_id = item.attrib.get("id", "")
            href = item.attrib.get("href", "")
            if item_id and href:
                manifest[item_id] = {
                    "href": href,
                    "media_type": item.attrib.get("media-type", ""),
                    "properties": item.attrib.get("properties", ""),
                }

        cover_href = ""
        for item in opf.iter():
            if item.tag.rsplit("}", 1)[-1] == "meta" and item.attrib.get("name") == "cover":
                cover_id = item.attrib.get("content", "")
                if cover_id in manifest:
                    cover_href = manifest[cover_id]["href"]
                    break

        if not cover_href:
            for item in manifest.values():
                if "cover-image" in item["properties"].split():
                    cover_href = item["href"]
                    break

        if not cover_href:
            for item in opf.iter():
                if item.tag.rsplit("}", 1)[-1] == "reference":
                    if "cover" in item.attrib.get("type", "").lower():
                        cover_href = item.attrib.get("href", "")
                        if cover_href:
                            break

        if not cover_href:
            for item in manifest.values():
                if item["media_type"].startswith("image/") and "cover" in item["href"].lower():
                    cover_href = item["href"]
                    break

        if not cover_href:
            return None
        try:
            resolved_cover = resolve_archive_path(rootfile, cover_href)
            data = archive.read(resolved_cover)
            data = unwrap_cover_reference(data, resolved_cover, archive)
            if data.lstrip().startswith((b"<html", b"<svg", b"<?xml")):
                for item in manifest.values():
                    if (
                        item["media_type"].startswith("image/")
                        and not item["media_type"].endswith("svg+xml")
                    ):
                        try:
                            fallback = archive.read(
                                resolve_archive_path(rootfile, item["href"])
                            )
                            if not fallback.lstrip().startswith(b"<"):
                                return fallback
                        except (KeyError, OSError, zipfile.BadZipFile):
                            continue
            return data
        except (KeyError, OSError, zipfile.BadZipFile):
            return None
    finally:
        archive.close()


def make_thumbnail(data: bytes) -> tuple[bytes, str] | None:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (247, 248, 245))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        image.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=82, optimize=True, progressive=True)
        return output.getvalue(), "epub"
    except (OSError, ValueError):
        return None


def make_image_thumbnail(path: Path) -> tuple[bytes, str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    result = make_thumbnail(data)
    if result:
        return result[0], "adjacent-image"
    return None


def index_adjacent_images(root: Path) -> dict[tuple[str, str], Path]:
    images: dict[tuple[str, str], Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in {".git", "node_modules", ".book-library", "_书库整理"}
            and not name.startswith("._")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in IMAGE_SUFFIXES and not filename.startswith("._"):
                key = (str(path.parent).casefold(), text_key(path.stem))
                images.setdefault(key, path)
    return images


def process_book(
    relative_path: str,
    root: Path,
    covers_dir: Path,
    adjacent_images: dict[tuple[str, str], Path],
) -> tuple[str, dict | None, str]:
    book_path = root / relative_path
    result = None
    if book_path.suffix.lower() == ".epub":
        cover_data = epub_cover_bytes(book_path)
        if cover_data:
            result = make_thumbnail(cover_data)
    if result is None:
        key = (str(book_path.parent).casefold(), text_key(book_path.stem))
        image_path = adjacent_images.get(key)
        if image_path:
            result = make_image_thumbnail(image_path)
    if result is None:
        return relative_path, None, "no-cover"

    data, source = result
    digest = hashlib.sha1(data).hexdigest()
    target_relative = Path("covers") / digest[:2] / f"{digest}.jpg"
    target = covers_dir.parent / target_relative
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return relative_path, {"file": target_relative.as_posix(), "source": source}, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=library_paths.ROOT)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=library_paths.EXPORTS_DIR / "图书总目录.csv",
    )
    parser.add_argument(
        "--covers",
        type=Path,
        default=library_paths.COVERS_DIR,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.CACHE_DIR / "cover_manifest.json",
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with args.catalog.open(encoding="utf-8-sig", newline="") as stream:
        relative_paths = [
            row["相对路径"]
            for row in csv.DictReader(stream)
            if row.get("相对路径")
        ]

    print("建立同目录图片索引...", flush=True)
    adjacent_images = index_adjacent_images(args.root)
    args.covers.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    covers: dict[str, dict] = {}
    status_counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(
                process_book,
                relative_path,
                args.root,
                args.covers,
                adjacent_images,
            )
            for relative_path in relative_paths
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            relative_path, cover, status = future.result()
            if cover:
                covers[relative_path] = cover
            status_counts[status] = status_counts.get(status, 0) + 1
            if index % 250 == 0:
                print(f"封面 {index}/{len(futures)}", flush=True)

    payload = {
        "covers": covers,
        "status_counts": status_counts,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "books": len(relative_paths),
                "covers": len(covers),
                "status_counts": status_counts,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
