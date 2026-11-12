#!/usr/bin/env python3
"""Build a read-only catalog and health report for the ebook collection."""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import library_store
import library_paths


PRIMARY_FORMATS = {
    ".epub",
    ".pdf",
    ".mobi",
    ".azw",
    ".azw3",
    ".djvu",
    ".uvz",
}
OTHER_DOC_FORMATS = {
    ".cbz",
    ".cbr",
    ".chm",
    ".doc",
    ".docx",
    ".fb2",
    ".html",
    ".lit",
    ".odt",
    ".pdb",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}
BOOK_FORMATS = PRIMARY_FORMATS | OTHER_DOC_FORMATS
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "_书库整理",
}
IGNORED_NAMES = {
    ".ds_store",
    "desktop.ini",
    "thumbs.db",
    "requirements.txt",
    "license.txt",
    "readme.txt",
    "cmakelists.txt",
}
COPY_MARKERS = ("副本", "复制", "copy", "下载", "未命名", "新建")
FORMAT_SUFFIX_RE = re.compile(
    r"(?i)(?:"
    r"\s*[\[(（【]\s*(?:z-library|zlib|扫描版?|影印版?|高清版?|电子版?|"
    r"true\s*pdf|ocr|可检索|全彩版?|中文版|英文版)\s*[\])）】]"
    r"|\s*[-_ ](?:副本|copy)\s*\d*\s*$"
    r"|\s*[\[(（]\s*\d+\s*[\])）]\s*$"
    r"|\s*[_-]\s*[a-f0-9]{8,}\s*$"
    r")"
)
LEADING_INDEX_RE = re.compile(r"^\s*\d{2,4}[-_.、\s]+")
AUTHOR_SPLIT_RE = re.compile(r"\s+-\s+")
ISBN_RE = re.compile(r"(?i)(?:urn:isbn:)?((?:97[89][-\s]?)?\d[\d\s-]{8,16}[\dX])")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z]{3,}")
BAD_TITLE_RE = re.compile(
    r"(?i)^(?:"
    r"subject|author|title|creator|untitled|print|index|document|"
    r"ssreader\s+print|全页传真图片|无标题|未命名|"
    r"microsoft(?:\s+word)?(?:\s+document)?|"
    r"iso\s+15930.*|adobe\s+acrobat.*"
    r")\s*[:：]?\s*$"
)
BAD_AUTHOR_RE = re.compile(
    r"(?i)^(?:author|unknown|未知|administrator|microsoft\s+office\s+user|"
    r"microsoft|adobe|user)\s*[:：]?$"
)


@dataclass
class Book:
    path: Path
    rel: Path
    size: int
    modified: float
    raw_title: str
    title: str
    author: str = ""
    language: str = ""
    pages: str = ""
    isbn: str = ""
    publisher: str = ""
    published: str = ""
    metadata_source: str = ""
    collection: str = ""
    category: str = ""
    subcategory: str = ""
    shelf: str = ""
    smart_subcategory: str = ""
    tags: str = ""
    flags: list[str] = field(default_factory=list)
    exact_group: str = ""
    exact_count: int = 0
    title_group: str = ""
    title_count: int = 0
    douban_rating: float | None = None
    douban_count: int | None = None
    douban_url: str = ""
    douban_match_type: str = ""
    cover_file: str = ""

    @property
    def ext(self) -> str:
        return self.path.suffix.lower().lstrip(".")

    @property
    def title_key(self) -> str:
        text = normalize_title(self.title)
        if not text:
            text = normalize_title(self.raw_title)
        author = normalize_title(self.author)
        return f"{text}|{author}" if text else ""

    @property
    def filename_language(self) -> str:
        stem = clean_text(self.path.stem)
        if CJK_RE.search(stem):
            return "中文"
        if ASCII_WORD_RE.search(stem):
            return "英文"
        return "无法判断"


def configure_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\x00", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def clean_title(value: str) -> str:
    value = clean_text(value)
    value = FORMAT_SUFFIX_RE.sub("", value)
    value = LEADING_INDEX_RE.sub("", value)
    value = re.sub(r"\s*[-_ ](?:副本|copy)\s*\d*\s*$", "", value, flags=re.I)
    return value.strip(" ._-")


def is_usable_metadata(value: str, *, title: bool) -> bool:
    value = clean_text(value)
    if not value:
        return False
    pattern = BAD_TITLE_RE if title else BAD_AUTHOR_RE
    if pattern.match(value):
        return False
    if title and not (CJK_RE.search(value) or re.search(r"[A-Za-z]{2,}", value)):
        return False
    if title and len(value) > 300:
        return False
    return True


PDF_BAD_TITLE_RE = re.compile(
    r"(?i)^(?:"
    r"(?:creator|author|subject|producer|title)\s*[:：]|"
    r"microsoft\s+word\s*-|powerpoint(?:\s+presentation|\s+演示文稿)|"
    r"adobe\s+(?:acrobat|indesign)|ssreader|sursenreader|"
    r"book\.dvi|untitled|未命名文档|file://|https?://|"
    r"bingdian\d*\.com|hongbaoshu\.net|"
    r"第?\s*\d+\s*章.*\.(?:indd|docx?)"
    r")"
)
PDF_GENERIC_TITLES = {
    "序",
    "今天",
    "中华人民共和国",
    "清华大学出版社",
    "目录",
    "前言",
    "序言",
}
PDF_GENERIC_DIRS = {
    "10兆以上",
    "4兆以上",
    "2兆以上",
    "1兆以上",
    "0.5兆以上",
    "0.5兆以下",
    "书籍",
    "新建文件夹",
    "用所选项目新建的文件夹",
    "pdf",
    "mobi",
    "epub",
    "azw3",
    "其他",
}
MOJIBAKE_MARKERS_RE = re.compile(r"[ÃÂÐÑÔÕÖ×ØÙÚÛÜÝÞßæçèéêëìíîïðñòóôõö]")


def _text_score(value: str) -> tuple[int, int]:
    cjk = len(CJK_RE.findall(value))
    replacement = value.count("�") + len(MOJIBAKE_MARKERS_RE.findall(value))
    return cjk, -replacement


def repair_pdf_mojibake(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    hex_match = re.fullmatch(r"<([0-9A-Fa-f]{8,})>", value)
    if hex_match:
        try:
            value = bytes.fromhex(hex_match.group(1)).decode("gb18030", errors="replace")
        except ValueError:
            pass

    candidates = [value]
    if any(ord(char) <= 255 for char in value):
        for encoding in ("gb18030", "utf-8"):
            try:
                candidates.append(value.encode("latin1").decode(encoding))
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return clean_text(max(candidates, key=_text_score))


def fallback_pdf_title(path: Path) -> str:
    title, _ = split_filename(path.stem)
    title = re.sub(r"^\d{5,}[-_\s]*", "", title)
    title = re.sub(r"(?i)[_-](?:text|ocr|scan|final)$", "", title).strip()
    title = clean_title(title)
    normalized = normalize_title(title)
    if normalized and normalized not in PDF_GENERIC_TITLES and len(normalized) >= 3:
        return title

    for parent in list(path.parents)[:4]:
        candidate = clean_title(parent.name)
        normalized = normalize_title(candidate)
        if (
            normalized
            and normalized not in PDF_GENERIC_TITLES
            and candidate.casefold() not in PDF_GENERIC_DIRS
            and len(normalized) >= 3
        ):
            return candidate
    return title or path.stem


def sanitize_pdf_title(value: str, path: Path) -> str:
    repaired = repair_pdf_mojibake(value)
    if re.match(
        r"(?i)^\((?:microsoft\s+word|creator|author|subject|producer|title)\b",
        repaired,
    ):
        repaired = repaired[1:]
        if repaired.endswith(")"):
            repaired = repaired[:-1]
    repaired = repaired.strip()
    repaired = re.sub(
        r"(?i)^(?:creator|author|subject|producer|title)\s*[:：]\s*",
        "",
        repaired,
    )
    repaired = re.sub(r"(?i)^microsoft\s+word\s*-\s*", "", repaired)
    repaired = re.sub(r"\.(?:docx?|indd|dvi|pdf)$", "", repaired, flags=re.I)
    repaired = clean_title(repaired).strip()
    normalized = normalize_title(repaired)

    if (
        not normalized
        or normalized in PDF_GENERIC_TITLES
        or PDF_BAD_TITLE_RE.search(value)
        or re.search(r"(?:\\[0-7]{3}){2,}", repaired)
        or repaired.startswith(("file://", "http://", "https://"))
        or (re.fullmatch(r"[A-Za-z0-9_.-]+", repaired or "") and len(normalized) < 5)
    ):
        return fallback_pdf_title(path)

    return repaired


def normalize_title(value: str) -> str:
    value = clean_title(value).casefold()
    value = re.sub(r"[\W_]+", "", value, flags=re.UNICODE)
    return value


def split_filename(stem: str) -> tuple[str, str]:
    stem = clean_title(stem)
    match = AUTHOR_SPLIT_RE.search(stem)
    if match and 0 < match.start() < len(stem) - 2:
        return stem[: match.start()].strip(), stem[match.end() :].strip()
    return stem, ""


def infer_language(title: str, relative_path: str, metadata_language: str) -> str:
    metadata_language = (metadata_language or "").lower()
    if metadata_language.startswith(("zh", "zho", "chi")):
        return "中文"
    if metadata_language.startswith(("en", "eng")):
        return "英文"
    if metadata_language:
        return "其他"
    has_cjk = bool(CJK_RE.search(title))
    has_ascii = bool(ASCII_WORD_RE.search(title))
    if has_cjk and has_ascii:
        return "中英混排"
    if has_cjk:
        return "中文"
    if has_ascii:
        return "英文"
    if "英文" in relative_path or "英语" in relative_path:
        return "英文"
    return "未知"


def should_skip(path: Path, root: Path) -> bool:
    name = path.name
    if name.lower() in IGNORED_NAMES or name.startswith("._"):
        return True
    if path.suffix.lower() not in BOOK_FORMATS:
        return True
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part.lower() in IGNORED_DIRS for part in rel_parts[:-1])


def iter_book_paths(root: Path) -> list[Path]:
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d.lower() not in IGNORED_DIRS and not d.startswith("._")
        ]
        current = Path(dirpath)
        for name in filenames:
            path = current / name
            if not should_skip(path, root):
                result.append(path)
    return result


def read_opf(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig", errors="replace")
        root = ET.fromstring(text)
    except (OSError, ET.ParseError):
        return {}
    values: dict[str, list[str]] = defaultdict(list)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        value = clean_text("".join(element.itertext()))
        if value and tag in {
            "title",
            "creator",
            "language",
            "publisher",
            "date",
            "identifier",
            "isbn",
        }:
            values[tag].append(value)
    result: dict[str, str] = {}
    if values.get("title"):
        result["title"] = values["title"][0]
    if values.get("creator"):
        result["author"] = "; ".join(dict.fromkeys(values["creator"]))
    if values.get("language"):
        result["language"] = values["language"][0]
    if values.get("publisher"):
        result["publisher"] = values["publisher"][0]
    if values.get("date"):
        result["published"] = values["date"][0]
    identifiers = values.get("identifier", []) + values.get("isbn", [])
    for identifier in identifiers:
        match = ISBN_RE.search(identifier)
        if match:
            result["isbn"] = re.sub(r"[-\s]", "", match.group(1))
            break
    return result


def read_epub_metadata(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(
                (
                    element.attrib.get("full-path", "")
                    for element in container.iter()
                    if element.tag.rsplit("}", 1)[-1] == "rootfile"
                ),
                "",
            )
            if not rootfile:
                return {}
            opf = ET.fromstring(archive.read(rootfile))
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
        return {}
    values: dict[str, list[str]] = defaultdict(list)
    for element in opf.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        value = clean_text("".join(element.itertext()))
        if value and tag in {
            "title",
            "creator",
            "language",
            "publisher",
            "date",
            "identifier",
            "isbn",
        }:
            values[tag].append(value)
    result: dict[str, str] = {}
    if values.get("title"):
        result["title"] = values["title"][0]
    if values.get("creator"):
        result["author"] = "; ".join(dict.fromkeys(values["creator"]))
    if values.get("language"):
        result["language"] = values["language"][0]
    if values.get("publisher"):
        result["publisher"] = values["publisher"][0]
    if values.get("date"):
        result["published"] = values["date"][0]
    identifiers = values.get("identifier", []) + values.get("isbn", [])
    for identifier in identifiers:
        match = ISBN_RE.search(identifier)
        if match:
            result["isbn"] = re.sub(r"[-\s]", "", match.group(1))
            break
    return result


def read_pdf_metadata(path: Path, pdfinfo_path: str) -> dict[str, str]:
    if not pdfinfo_path:
        return {}
    try:
        completed = subprocess.run(
            [pdfinfo_path, "-enc", "UTF-8", "-isodates", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    text = completed.stdout.decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for key, value in re.findall(r"(?m)^([^:\r\n]+):\s*(.*)$", text):
        fields[key.strip().lower()] = value.strip()
    result: dict[str, str] = {}
    if fields.get("title"):
        result["title"] = fields["title"]
    if fields.get("author"):
        result["author"] = fields["author"]
    if fields.get("pages"):
        result["pages"] = fields["pages"]
    return result


def find_nearest_opf(path: Path, opf_cache: dict[Path, dict[str, str]]) -> dict[str, str]:
    current = path.parent
    for _ in range(3):
        candidate = current / "metadata.opf"
        if candidate in opf_cache:
            return opf_cache[candidate]
        if current.parent == current:
            break
        current = current.parent
    return {}


def choose_shelf(path_text: str, title: str) -> str:
    text = f"{path_text} {title}".casefold()
    rules: list[tuple[str, tuple[str, ...]]] = [
        (
            "计算机",
            (
                "ai学习",
                "人工智能",
                "机器学习",
                "深度学习",
                "大模型",
                "llm",
                "langchain",
                "神经网络",
                "python",
                "数据编程",
                "1-互联网",
                "算法",
                "计算机",
                "编程",
                "量化交易",
            ),
        ),
        ("网络文学", ("7-网文", "网文", "晋江", "网络文学")),
        ("写作与表达", ("写作", "描写辞典", "故事写作", "文案")),
        ("艺术与摄影", ("摄影", "5-艺术", "书法", "绘画", "名画", "篆刻")),
        (
            "语言学习",
            (
                "9-英语",
                "英语学习",
                "英语语法",
                "英语词汇",
                "英语口语",
                "雅思",
                "托福",
            ),
        ),
        (
            "历史与传统文化",
            (
                "国学",
                "历史",
                "资治通鉴",
                "史记",
                "汉书",
                "中国史",
                "世界史",
                "民国",
                "帝国",
                "王朝",
                "二战",
                "一战",
                "茅盾文学奖",
                "传记",
                "古籍",
            ),
        ),
        (
            "商业与经济",
            (
                "商业",
                "创业",
                "证券",
                "理财",
                "金融",
                "经济",
                "政经",
                "投资",
                "资本",
                "货币",
                "市场",
                "企业",
                "管理",
                "营销",
                "领导力",
                "财富",
                "股票",
                "基金",
            ),
        ),
        (
            "科学与自然",
            (
                "科学",
                "科普",
                "数学",
                "几何",
                "物理",
                "化学",
                "生物",
                "基因",
                "进化",
                "量子",
                "宇宙",
                "地球",
                "生命",
                "医学",
                "健康",
                "睡眠",
                "饮食",
                "运动",
                "身体健康",
            ),
        ),
        (
            "哲学与心理",
            (
                "哲思",
                "哲理",
                "哲学",
                "伦理",
                "逻辑",
                "心理",
                "人格",
                "认知",
                "情绪",
                "焦虑",
                "习惯",
            ),
        ),
        (
            "文学",
            (
                "文学",
                "小说",
                "名著",
                "诗歌",
                "散文",
                "随笔",
                "文集",
                "全集",
                "三毛",
                "冯唐",
                "茅盾文学奖",
                "东野圭吾",
                "村上春树",
                "余华",
                "莫言",
                "王小波",
                "张爱玲",
                "沈从文",
                "路遥",
                "刘慈欣",
                "严歌苓",
                "马尔克斯",
                "卡尔维诺",
                "博尔赫斯",
                "毛姆",
                "太宰治",
                "加缪",
                "陀思妥耶夫斯基",
                "托尔斯泰",
                "狄更斯",
                "奥斯汀",
            ),
        ),
        ("外语读物", ("英文", "9-英语", "英文原版", "双语")),
        (
            "教材与考试",
            ("教材", "教程", "考试", "考研", "习题", "高等数学", "概率论", "线性代数"),
        ),
        (
            "人文与社会",
            (
                "社科",
                "政治",
                "社会学",
                "文化",
                "法律",
                "思想",
                "汉译世界学术名著",
            ),
        ),
    ]
    for shelf, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return shelf
    if "教育" in text or "课程" in text:
        return "教材与考试"
    if "教程" in text or "指南" in text:
        return "实用工具书"
    return "综合与待归类"


CATEGORY_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    "计算机": (
        ("人工智能", 6), ("机器学习", 6), ("深度学习", 6), ("大模型", 6),
        ("LLM", 5), ("ChatGPT", 4), ("GPT", 3), ("RAG", 4), ("Agent", 3),
        ("神经网络", 5), ("自然语言处理", 5), ("计算机视觉", 5),
        ("Python", 3), ("编程", 4), ("算法", 3), ("代码", 2),
        ("TensorFlow", 4), ("PyTorch", 4), ("数据科学", 4),
        ("Linux", 3), ("数据库", 3), ("架构", 2), ("计算机", 3),
    ),
    "文学": (
        ("小说", 5), ("文学", 5), ("名著", 3), ("诗歌", 4), ("散文", 4),
        ("随笔", 3), ("文集", 3), ("全集", 2), ("东野圭吾", 4),
        ("村上春树", 4), ("余华", 4), ("莫言", 4), ("张爱玲", 4),
        ("沈从文", 4), ("路遥", 4), ("刘慈欣", 4), ("马尔克斯", 4),
        ("卡尔维诺", 4), ("毛姆", 4), ("加缪", 4),
    ),
    "网络文学": (
        ("网文", 6), ("网络文学", 6), ("晋江", 5), ("玄幻", 4),
        ("仙侠", 4), ("穿越", 3), ("都市", 2), ("修真", 4),
    ),
    "人文与社会": (
        ("社会学", 5), ("政治", 4), ("法律", 4), ("文化", 3),
        ("思想", 3), ("社会", 3), ("人类学", 4),
    ),
    "历史与传统文化": (
        ("历史", 5), ("国学", 5), ("史记", 5), ("资治通鉴", 6),
        ("汉书", 5), ("中国史", 5), ("世界史", 5), ("王朝", 3),
        ("帝国", 3), ("民国", 3), ("二战", 4), ("一战", 4),
        ("传记", 3), ("古籍", 4), ("文明史", 4),
    ),
    "商业与经济": (
        ("商业", 5), ("经济", 5), ("金融", 5), ("投资", 5),
        ("创业", 4), ("管理", 3), ("营销", 4), ("财务", 4),
        ("证券", 4), ("股票", 4), ("基金", 3), ("货币", 3),
        ("企业", 3), ("领导力", 3), ("财富", 3), ("产品经理", 4),
        ("洞察报告", 5), ("品类", 3), ("行业报告", 4),
    ),
    "科学与自然": (
        ("科学", 4), ("科普", 5), ("物理", 5), ("化学", 5),
        ("生物", 5), ("数学", 5), ("几何", 4), ("宇宙", 4),
        ("量子", 5), ("进化", 4), ("基因", 4), ("医学", 4),
        ("健康", 3), ("睡眠", 3), ("饮食", 2), ("生命", 3),
    ),
    "哲学与心理": (
        ("哲学", 6), ("伦理", 5), ("逻辑", 5), ("心理", 6),
        ("认知", 4), ("人格", 4), ("情绪", 3), ("思想", 3),
    ),
    "艺术与摄影": (
        ("摄影", 6), ("书法", 6), ("绘画", 5), ("名画", 5),
        ("艺术", 4), ("设计", 3), ("篆刻", 5), ("构图", 4),
        ("色彩", 3), ("相机", 3), ("镜头", 3), ("后期", 2),
    ),
    "写作与表达": (
        ("写作", 6), ("文案", 5), ("表达", 3), ("修辞", 4),
        ("故事写作", 5), ("叙事", 4), ("编辑", 3), ("演讲", 3),
    ),
    "语言学习": (
        ("英语学习", 6), ("英语语法", 6), ("英语词汇", 6),
        ("口语", 4), ("雅思", 5), ("托福", 5), ("双语", 3),
        ("英文原版", 4), ("单词", 3),
    ),
    "教材与考试": (
        ("教材", 5), ("教程", 4), ("考试", 5), ("考研", 5),
        ("习题", 4), ("题库", 4), ("入门", 2), ("指南", 2),
        ("手册", 2), ("课程", 3), ("线性代数", 6), ("高等数学", 6),
        ("概率论", 6), ("微积分", 6), ("数理统计", 6),
    ),
}
SUBCATEGORY_RULES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "计算机": (
        ("人工智能", ("人工智能", "AI", "机器学习", "深度学习", "神经网络", "大模型", "LLM", "ChatGPT", "RAG", "Agent")),
        ("编程开发", ("编程", "代码", "开发", "Python", "Java", "C++", "Linux", "数据库", "前端", "后端", "Git")),
        ("算法与数据", ("算法", "数据结构", "数据分析", "数据科学", "统计", "量化")),
        ("系统与网络", ("计算机", "操作系统", "网络", "架构", "安全", "运维")),
    ),
    "文学": (
        ("中国文学", ("中国", "当代", "现代", "华语", "莫言", "余华", "路遥", "张爱玲", "沈从文")),
        ("外国文学", ("外国", "译", "英文", "马尔克斯", "村上春树", "毛姆", "加缪", "托尔斯泰")),
        ("科幻奇幻", ("科幻", "奇幻", "太空", "魔法", "刘慈欣", "银河")),
        ("悬疑推理", ("推理", "悬疑", "侦探", "东野圭吾", "谋杀")),
        ("诗歌散文", ("诗", "散文", "随笔", "词集")),
        ("经典名著", ("名著", "经典", "全集", "文集")),
    ),
    "网络文学": (
        ("玄幻仙侠", ("玄幻", "仙侠", "修真", "修仙")),
        ("都市言情", ("都市", "言情", "爱情")),
        ("历史军事", ("历史", "军事", "战争")),
        ("科幻悬疑", ("科幻", "悬疑", "灵异")),
        ("游戏竞技", ("游戏", "竞技", "电竞")),
    ),
    "人文与社会": (
        ("社会学", ("社会学", "社会", "人类学")),
        ("政治法律", ("政治", "法律", "制度", "国家")),
        ("文化研究", ("文化", "文明", "宗教", "民俗")),
        ("社会议题", ("社会问题", "公共", "城市", "家庭", "人口")),
    ),
    "历史与传统文化": (
        ("中国历史", ("中国史", "中国历史", "史记", "资治通鉴", "汉书", "王朝", "帝国")),
        ("世界历史", ("世界史", "世界历史", "二战", "一战", "欧洲史", "美国史")),
        ("国学古籍", ("国学", "古籍", "经史子集", "古典")),
        ("人物传记", ("传记", "自传", "回忆录", "人物")),
        ("历史通识", ("历史", "文明史", "简史")),
    ),
    "商业与经济": (
        ("经济金融", ("经济", "金融", "货币", "银行")),
        ("投资理财", ("投资", "理财", "证券", "股票", "基金")),
        ("企业管理", ("企业", "管理", "领导力", "组织")),
        ("创业营销", ("创业", "营销", "品牌", "市场")),
        ("产品运营", ("产品经理", "运营", "用户增长", "洞察报告", "品类")),
    ),
    "科学与自然": (
        ("物理学", ("物理", "量子", "相对论", "力学", "电磁", "热力学")),
        ("化学", ("化学", "元素", "分子")),
        ("生物学", ("生物", "基因", "进化", "细胞", "生态")),
        ("数学", ("数学", "几何", "代数", "微积分", "概率论", "数理统计")),
        ("天文学", ("天文", "宇宙", "星系", "行星", "黑洞")),
        ("地球科学", ("地球", "地质", "地理", "气候", "海洋")),
        ("医学健康", ("医学", "健康", "睡眠", "饮食", "身体")),
        ("科学通识", ("科学", "科普", "科学史", "自然")),
    ),
    "哲学与心理": (
        ("哲学", ("哲学", "伦理", "逻辑")),
        ("心理学", ("心理", "人格", "认知", "情绪", "焦虑", "习惯")),
        ("逻辑学", ("逻辑", "推理", "思维")),
        ("伦理学", ("伦理", "道德")),
        ("认知科学", ("认知", "意识", "脑科学")),
    ),
    "艺术与摄影": (
        ("摄影", ("摄影", "相机", "镜头", "构图", "曝光", "后期")),
        ("书法", ("书法", "字帖", "篆刻")),
        ("绘画", ("绘画", "名画", "国画", "油画")),
        ("艺术史", ("艺术史", "艺术", "鉴赏")),
        ("设计", ("设计", "色彩", "版式")),
    ),
    "写作与表达": (
        ("写作方法", ("写作", "创作方法", "写作课")),
        ("叙事技巧", ("叙事", "故事", "情节", "人物塑造")),
        ("文案表达", ("文案", "表达", "修辞")),
        ("演讲沟通", ("演讲", "沟通", "谈判")),
        ("编辑出版", ("编辑", "出版", "校对")),
    ),
    "语言学习": (
        ("英语", ("英语", "英文")),
        ("其他语言", ("日语", "法语", "德语", "韩语")),
        ("词汇语法", ("词汇", "单词", "语法")),
        ("听说读写", ("口语", "听力", "阅读", "写作")),
        ("考试英语", ("雅思", "托福", "四六级")),
    ),
    "外语读物": (
        ("英文小说", ("英文原版", "小说", "fiction")),
        ("英文非虚构", ("英文", "非虚构", "历史", "传记")),
        ("双语读物", ("双语", "中英对照", "英汉对照")),
        ("原版教材", ("教材", "课本", "教程")),
    ),
    "教材与考试": (
        ("数学", ("数学", "代数", "微积分", "概率论")),
        ("英语", ("英语", "雅思", "托福")),
        ("考研", ("考研", "上岸")),
        ("职业考试", ("职业", "资格", "考试")),
        ("学科教材", ("教材", "课本", "教程")),
    ),
    "实用工具书": (
        ("工具软件", ("软件", "工具", "Excel", "Office")),
        ("生活指南", ("生活", "指南", "手册")),
        ("职场技能", ("职场", "技能", "效率")),
        ("参考手册", ("参考", "辞典", "手册")),
    ),
    "综合与待归类": (
        ("综合合集", ("合集", "套装", "全集")),
        ("资料文档", ("资料", "报告", "文档")),
    ),
}
SUBCATEGORY_FALLBACK = {
    "计算机": "其他计算机",
    "文学": "其他文学",
    "网络文学": "其他网文",
    "人文与社会": "其他人文社会",
    "历史与传统文化": "其他历史传统文化",
    "商业与经济": "其他商业经济",
    "科学与自然": "其他科学",
    "哲学与心理": "其他哲学心理",
    "艺术与摄影": "其他艺术",
    "写作与表达": "其他写作",
    "语言学习": "其他语言",
    "外语读物": "其他外语读物",
    "教材与考试": "其他教材考试",
    "实用工具书": "其他工具书",
    "综合与待归类": "待人工归类",
}
TAG_RULES: dict[str, tuple[str, ...]] = {
    "人工智能": (
        "人工智能", "AI", "机器学习", "深度学习", "大模型", "LLM", "ChatGPT",
        "GPT", "RAG", "Agent", "神经网络", "自然语言处理", "计算机视觉",
    ),
    "编程开发": (
        "编程", "代码", "开发", "Python", "Java", "C++", "Linux", "数据库",
        "算法", "架构", "前端", "后端", "Git", "TensorFlow", "PyTorch",
    ),
    "文学": ("小说", "文学", "诗歌", "散文", "随笔", "文集"),
    "网络小说": ("网文", "网络文学", "晋江", "玄幻", "仙侠", "穿越", "修真"),
    "历史": ("历史", "史记", "资治通鉴", "中国史", "世界史", "王朝", "帝国"),
    "哲学": ("哲学", "伦理", "逻辑", "思想"),
    "心理": ("心理", "人格", "认知", "情绪", "焦虑", "习惯"),
    "商业管理": (
        "商业", "管理", "创业", "营销", "领导力", "企业", "产品经理",
        "洞察报告", "品类", "行业报告",
    ),
    "经济金融": ("经济", "金融", "投资", "证券", "股票", "基金", "财富", "货币"),
    "科学通识": ("科学", "科普", "科学史", "科技史", "自然"),
    "物理学": ("物理", "量子", "相对论", "力学", "电磁", "热力学"),
    "化学": ("化学", "元素", "分子", "化学反应"),
    "生物学": ("生物", "基因", "进化", "细胞", "生态", "遗传"),
    "数学": ("数学", "几何", "代数", "微积分", "概率论", "数理统计"),
    "天文学": ("天文", "宇宙", "星系", "行星", "黑洞"),
    "地球科学": ("地球", "地质", "地理", "气候", "海洋"),
    "医学健康": ("医学", "健康", "睡眠", "饮食", "身体", "运动"),
    "摄影": ("摄影", "相机", "镜头", "构图", "曝光", "后期"),
    "书法绘画": ("书法", "绘画", "名画", "篆刻", "字帖"),
    "写作": ("写作", "文案", "叙事", "修辞", "表达", "演讲"),
    "语言学习": ("英语学习", "英语语法", "英语词汇", "口语", "雅思", "托福", "单词"),
    "双语": ("双语", "中英对照", "英汉对照"),
    "教材/教程": ("教材", "教程", "课程", "入门", "指南", "手册"),
    "考试/考研": ("考试", "考研", "习题", "题库"),
    "传记": ("传记", "自传", "回忆录"),
    "精校版": ("精校", "精校版", "校订", "校对版"),
    "扫描版": ("扫描版", "扫描件", "影印版", "影印"),
    "套装/全集": ("套装", "全集", "全套"),
}


def _contains_keyword(text: str, keyword: str) -> bool:
    keyword = keyword.casefold()
    if re.fullmatch(r"[a-z0-9+#. -]+", keyword) and len(keyword) <= 5:
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def infer_tags(title: str, author: str, path_text: str) -> str:
    content_text = f"{title} {author}".casefold()
    path_lower = path_text.casefold()
    tags = [
        tag
        for tag, keywords in TAG_RULES.items()
        if any(_contains_keyword(content_text, keyword) for keyword in keywords)
    ]
    if not tags:
        tags = [
            tag
            for tag, keywords in TAG_RULES.items()
            if any(_contains_keyword(path_lower, keyword) for keyword in keywords)
        ]
    return "；".join(tags[:8])


def classify_subcategory(
    category: str,
    title: str,
    author: str,
    path_text: str,
) -> str:
    content_text = f"{title} {author}".casefold()
    path_lower = path_text.casefold()
    best_name = SUBCATEGORY_FALLBACK.get(category, "待人工归类")
    best_score = 0
    for name, keywords in SUBCATEGORY_RULES.get(category, ()):
        score = 0
        for keyword in keywords:
            if _contains_keyword(content_text, keyword):
                score += 3
            if _contains_keyword(path_lower, keyword):
                score += 1
        if score > best_score:
            best_name = name
            best_score = score
    return best_name


def classify_book(title: str, author: str, path_text: str) -> tuple[str, str, str]:
    title_text = f"{title} {author}".casefold()
    path_lower = path_text.casefold()
    scores: dict[str, float] = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0.0
        for keyword, weight in keywords:
            if _contains_keyword(title_text, keyword):
                score += weight * 3
            if _contains_keyword(path_lower, keyword):
                score += weight
        scores[category] = score

    best_category, best_score = max(scores.items(), key=lambda item: item[1])
    fallback = choose_shelf(path_text, "")
    if best_score >= 5:
        category = best_category
    elif best_score >= 2 and fallback == "综合与待归类":
        category = best_category
    else:
        category = fallback
    smart_subcategory = classify_subcategory(category, title, author, path_text)
    return category, smart_subcategory, infer_tags(title, author, path_text)


def add_quality_flags(book: Book) -> None:
    name = book.path.stem
    rel = str(book.rel)
    if any(marker.casefold() in name.casefold() for marker in COPY_MARKERS):
        book.flags.append("疑似副本/临时名")
    if re.search(r"[\[(（]\s*\d+\s*[\])）]\s*$", name):
        book.flags.append("带下载编号")
    if "z-library" in name.casefold() or "zlib" in name.casefold():
        book.flags.append("带下载站水印")
    if "新建文件夹" in rel:
        book.flags.append("位于临时目录")
    if len(name) > 90:
        book.flags.append("文件名过长")
    if book.size == 0:
        book.flags.append("空文件")
    if book.ext == "pdf" and book.size < 50_000:
        book.flags.append("PDF异常偏小")
    if not book.author:
        book.flags.append("作者未知")


def build_base_book(path: Path, root: Path) -> Book:
    stat = path.stat()
    rel = path.relative_to(root)
    title, author = split_filename(path.stem)
    parts = rel.parts
    collection = parts[0] if parts else ""
    if collection == "【00总文库】":
        category = parts[1] if len(parts) > 2 else collection
        subcategory = parts[2] if len(parts) > 3 else ""
    else:
        category = parts[1] if len(parts) > 2 else collection
        subcategory = parts[2] if len(parts) > 3 else ""
    shelf, smart_subcategory, tags = classify_book(title, author, str(rel))
    return Book(
        path=path,
        rel=rel,
        size=stat.st_size,
        modified=stat.st_mtime,
        raw_title=clean_title(path.stem),
        title=title,
        author=author,
        collection=collection,
        category=category,
        subcategory=subcategory,
        shelf=shelf,
        smart_subcategory=smart_subcategory,
        tags=tags,
    )


def enrich_metadata(
    books: list[Book], root: Path, pdfinfo_path: str, workers: int
) -> None:
    opf_paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d.lower() not in IGNORED_DIRS and not d.startswith("._")
        ]
        if "metadata.opf" in filenames:
            opf_paths.append(Path(dirpath) / "metadata.opf")
    opf_cache = {path: read_opf(path) for path in opf_paths}

    epub_books = [book for book in books if book.ext == "epub"]
    pdf_books = [book for book in books if book.ext == "pdf"]

    def epub_task(book: Book) -> tuple[Book, dict[str, str]]:
        return book, read_epub_metadata(book.path)

    def pdf_task(book: Book) -> tuple[Book, dict[str, str]]:
        return book, read_pdf_metadata(book.path, pdfinfo_path)

    def apply_result(book: Book, metadata: dict[str, str]) -> None:
        if metadata.get("title"):
            if book.ext == "pdf":
                candidate = sanitize_pdf_title(metadata["title"], book.path)
            else:
                candidate = clean_title(metadata["title"])
            if candidate and is_usable_metadata(candidate, title=True):
                book.title = candidate
        if metadata.get("author"):
            candidate_author = clean_text(metadata["author"])
            if is_usable_metadata(candidate_author, title=False):
                book.author = candidate_author
        book.language = metadata.get("language", "")
        book.pages = metadata.get("pages", "")
        book.isbn = metadata.get("isbn", "")
        book.publisher = metadata.get("publisher", "")
        book.published = metadata.get("published", "")
        book.metadata_source = "内嵌元数据"

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(epub_task, book) for book in epub_books]
        futures += [pool.submit(pdf_task, book) for book in pdf_books]
        for future in as_completed(futures):
            book, metadata = future.result()
            if metadata:
                apply_result(book, metadata)
            completed += 1
            if completed % 250 == 0:
                print(f"元数据 {completed}/{len(futures)}", flush=True)

    for book in books:
        if book.metadata_source:
            continue
        metadata = find_nearest_opf(book.path, opf_cache)
        if metadata:
            apply_result(book, metadata)
            book.metadata_source = "同级OPF元数据"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def mark_exact_duplicates(books: list[Book], workers: int) -> tuple[int, int]:
    size_groups: dict[int, list[Book]] = defaultdict(list)
    for book in books:
        size_groups[book.size].append(book)
    candidates = [
        book
        for group in size_groups.values()
        if len(group) > 1
        for book in group
    ]
    print(f"按大小筛出哈希候选 {len(candidates)} 个", flush=True)
    hashes: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(file_sha256, book.path): book for book in candidates}
        for index, future in enumerate(as_completed(futures), start=1):
            book = futures[future]
            try:
                hashes[book.path] = future.result()
            except OSError:
                hashes[book.path] = ""
            if index % 250 == 0:
                print(f"哈希 {index}/{len(candidates)}", flush=True)

    hash_groups: dict[str, list[Book]] = defaultdict(list)
    for book in candidates:
        digest = hashes.get(book.path, "")
        if digest:
            hash_groups[digest].append(book)
    duplicate_groups = [group for group in hash_groups.values() if len(group) > 1]
    duplicate_groups.sort(key=lambda group: (-group[0].size, str(group[0].rel)))
    duplicate_bytes = 0
    for index, group in enumerate(duplicate_groups, start=1):
        group_id = f"D{index:05d}"
        duplicate_bytes += group[0].size * (len(group) - 1)
        for book in group:
            book.exact_group = group_id
            book.exact_count = len(group)
    return len(duplicate_groups), duplicate_bytes


def mark_title_duplicates(books: list[Book]) -> int:
    title_groups: dict[str, list[Book]] = defaultdict(list)
    for book in books:
        key = book.title_key
        if key and len(key.split("|", 1)[0]) >= 3:
            title_groups[key].append(book)
    duplicate_groups = [group for group in title_groups.values() if len(group) > 1]
    duplicate_groups.sort(key=lambda group: (-len(group), group[0].title_key))
    for index, group in enumerate(duplicate_groups, start=1):
        group_id = f"T{index:05d}"
        for book in group:
            book.title_group = group_id
            book.title_count = len(group)
    return len(duplicate_groups)


def attach_offline_assets(books: list[Book], output_dir: Path) -> None:
    ratings_path = library_paths.CACHE_DIR / "douban_ratings.json"
    covers_path = library_paths.CACHE_DIR / "cover_manifest.json"
    if not ratings_path.exists():
        ratings_path = library_paths.LEGACY_DIR / "douban_ratings.json"
    if not covers_path.exists():
        covers_path = library_paths.LEGACY_DIR / "cover_manifest.json"
    ratings: dict[str, dict[str, Any]] = {}
    covers: dict[str, dict[str, str]] = {}
    if ratings_path.exists():
        payload = json.loads(ratings_path.read_text(encoding="utf-8"))
        ratings = payload.get("ratings", {})
    if covers_path.exists():
        payload = json.loads(covers_path.read_text(encoding="utf-8"))
        covers = payload.get("covers", {})

    for book in books:
        rating = ratings.get(normalize_title(book.title))
        if rating and rating.get("status") == "matched":
            book.douban_rating = rating.get("rating")
            book.douban_count = rating.get("rating_count")
            book.douban_url = rating.get("subject_url", "")
            book.douban_match_type = rating.get("match_type", "")
        cover = covers.get(str(book.rel))
        if cover:
            book.cover_file = cover.get("file", "")


def load_books_from_catalog(path: Path, root: Path) -> list[Book]:
    books: list[Book] = []
    data_dir = path.parent.parent if path.parent.name == "exports" else path.parent
    db_path = data_dir / "library.db"
    if not db_path.exists():
        db_path = data_dir / "data" / "library.db"
    if db_path.exists():
        rows = library_store.load_catalog_rows(db_path)
    else:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    for row in rows:
        rel = Path(row["相对路径"])
        modified = datetime.strptime(row["修改时间"], "%Y-%m-%d %H:%M").timestamp()
        size_mb = float(row.get("大小MB") or 0)
        books.append(
            Book(
                path=root / rel,
                rel=rel,
                size=int(size_mb * 1024 * 1024),
                modified=modified,
                raw_title=clean_title((root / rel).stem),
                title=row["书名"],
                author=row["作者"],
                collection=row["现有总库"],
                category=row["现有分类"],
                subcategory=row["现有子类"],
                shelf=row["建议书架"],
                smart_subcategory=row.get("智能子类", ""),
                tags=row.get("标签", ""),
                flags=[
                    flag
                    for flag in (row.get("质量标记") or "").split("；")
                    if flag
                ],
                exact_group=row["精确重复组"],
                exact_count=int(row["精确重复数"] or 0),
                title_group=row["疑似同名组"],
                title_count=int(row["疑似同名数"] or 0),
                douban_rating=(
                    float(row["豆瓣评分"]) if row.get("豆瓣评分") else None
                ),
                douban_count=(
                    int(row["豆瓣评分人数"])
                    if row.get("豆瓣评分人数")
                    else None
                ),
                douban_url=row.get("豆瓣条目", ""),
                douban_match_type=row.get("豆瓣匹配方式", ""),
                cover_file=row.get("封面", ""),
            )
        )
    return books


def book_to_row(book: Book) -> dict[str, Any]:
    return {
        "书名": book.title,
        "作者": book.author,
        "格式": book.ext.upper(),
        "文件名语言": book.filename_language,
        "语言": infer_language(book.title, str(book.rel), book.language),
        "豆瓣评分": book.douban_rating if book.douban_rating is not None else "",
        "豆瓣评分人数": (
            book.douban_count if book.douban_count is not None else ""
        ),
        "豆瓣匹配方式": book.douban_match_type,
        "豆瓣条目": book.douban_url,
        "封面": book.cover_file,
        "标签": book.tags,
        "建议书架": book.shelf,
        "智能子类": book.smart_subcategory,
        "现有总库": book.collection,
        "现有分类": book.category,
        "现有子类": book.subcategory,
        "大小MB": round(book.size / 1024 / 1024, 2),
        "页数": book.pages,
        "ISBN": book.isbn,
        "出版社": book.publisher,
        "出版时间": book.published,
        "元数据来源": book.metadata_source or "文件名推断",
        "质量标记": "；".join(book.flags),
        "精确重复组": book.exact_group,
        "精确重复数": book.exact_count if book.exact_count else "",
        "疑似同名组": book.title_group,
        "疑似同名数": book.title_count if book.title_count else "",
        "修改时间": datetime.fromtimestamp(book.modified).strftime("%Y-%m-%d %H:%M"),
        "相对路径": str(book.rel),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.2f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    return f"{value / 1024:.0f} KB"


def generate_csv_reports(
    output_dir: Path,
    books: list[Book],
    duplicate_groups: int,
    duplicate_bytes: int,
    title_groups: int,
) -> None:
    exports_dir = output_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    rows = [book_to_row(book) for book in books]
    rows.sort(key=lambda row: (row["建议书架"], row["书名"].casefold()))
    fields = list(rows[0].keys()) if rows else ["书名"]
    write_csv(exports_dir / "图书总目录.csv", rows, fields)

    duplicate_rows = [row for row in rows if row["精确重复组"]]
    duplicate_rows.sort(
        key=lambda row: (row["精确重复组"], row["相对路径"].casefold())
    )
    write_csv(exports_dir / "精确重复文件.csv", duplicate_rows, fields)

    title_rows = [row for row in rows if row["疑似同名组"]]
    title_rows.sort(
        key=lambda row: (row["疑似同名组"], row["格式"], row["相对路径"].casefold())
    )
    write_csv(exports_dir / "疑似同名书目.csv", title_rows, fields)

    grouped_duplicates: dict[str, list[Book]] = defaultdict(list)
    for book in books:
        if book.exact_group:
            grouped_duplicates[book.exact_group].append(book)
    action_rows = []
    penalty_flags = {
        "疑似副本/临时名",
        "位于临时目录",
        "带下载编号",
        "带下载站水印",
        "文件名过长",
        "空文件",
    }
    for group_id, group in sorted(grouped_duplicates.items()):
        collection_rank = {
            "【00总文库】": 0,
            "精简-移动设备": 4,
            "新书": 3,
        }
        keeper = min(
            group,
            key=lambda book: (
                sum(flag in penalty_flags for flag in book.flags),
                collection_rank.get(book.collection, 1),
                len(book.rel.parts),
                len(str(book.rel)),
                str(book.rel).casefold(),
            ),
        )
        for book in sorted(group, key=lambda item: str(item.rel).casefold()):
            action_rows.append(
                {
                    "精确重复组": group_id,
                    "建议动作": "保留" if book is keeper else "移出待确认",
                    "书名": book.title,
                    "格式": book.ext.upper(),
                    "大小MB": round(book.size / 1024 / 1024, 2),
                    "同组文件数": len(group),
                    "相对路径": str(book.rel),
                }
            )
    write_csv(
        exports_dir / "建议去重清单.csv",
        action_rows,
        ["精确重复组", "建议动作", "书名", "格式", "大小MB", "同组文件数", "相对路径"],
    )

    shelf_counts = Counter(book.shelf for book in books)
    shelf_sizes = Counter()
    for book in books:
        shelf_sizes[book.shelf] += book.size
    shelf_rows = [
        {
            "建议书架": shelf,
            "文件数": count,
            "大小GB": round(shelf_sizes[shelf] / 1024**3, 2),
        }
        for shelf, count in shelf_counts.most_common()
    ]
    write_csv(
        exports_dir / "建议书架统计.csv",
        shelf_rows,
        ["建议书架", "文件数", "大小GB"],
    )


def generate_markdown_report(
    output_dir: Path,
    books: list[Book],
    duplicate_groups: int,
    duplicate_bytes: int,
    title_groups: int,
) -> None:
    format_counts = Counter(book.ext.upper() for book in books)
    format_sizes = Counter()
    for book in books:
        format_sizes[book.ext.upper()] += book.size
    collection_counts = Counter(book.collection for book in books)
    collection_sizes = Counter()
    for book in books:
        collection_sizes[book.collection] += book.size
    shelf_counts = Counter(book.shelf for book in books)
    flag_counts = Counter(flag for book in books for flag in book.flags)
    total_size = sum(book.size for book in books)
    rating_count = sum(1 for book in books if book.douban_rating is not None)
    cover_count = sum(1 for book in books if book.cover_file)

    lines = [
        "# 书库体检报告",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        "扫描方式：只读扫描；未移动、改名或删除任何原始文件。",
        "",
        "## 总体情况",
        "",
        f"- 书刊与资料文件：**{len(books):,}** 个",
        f"- 占用空间：**{format_bytes(total_size)}**",
        f"- 格式种类：**{len(format_counts)}** 种",
        f"- 精确重复组：**{duplicate_groups:,}** 组，可回收约 **{format_bytes(duplicate_bytes)}**",
        f"- 疑似同名组：**{title_groups:,}** 组（包含同书不同格式或版本）",
        f"- “作者未知”记录：**{flag_counts.get('作者未知', 0):,}** 个",
        f"- 已匹配豆瓣评分：**{rating_count:,}** 个文件（{rating_count / len(books) * 100:.1f}%）",
        f"- 已有本地封面：**{cover_count:,}** 个文件",
        "",
        "## 格式分布",
        "",
        "| 格式 | 文件数 | 大小 |",
        "|---|---:|---:|",
    ]
    for ext, count in format_counts.most_common():
        lines.append(f"| {ext} | {count:,} | {format_bytes(format_sizes[ext])} |")

    lines += [
        "",
        "## 现有目录分布",
        "",
        "| 顶层目录 | 书刊/资料文件 | 大小 |",
        "|---|---:|---:|",
    ]
    for collection, count in collection_counts.most_common():
        lines.append(
            f"| {collection} | {count:,} | {format_bytes(collection_sizes[collection])} |"
        )

    lines += [
        "",
        "## 建议书架分布",
        "",
        "| 建议书架 | 文件数 |",
        "|---|---:|",
    ]
    for shelf, count in shelf_counts.most_common():
        lines.append(f"| {shelf} | {count:,} |")

    lines += [
        "",
        "## 主要问题",
        "",
        "1. 主库与主题区、移动端精选区存在交叉，同一本书常出现 2 至 5 份。",
        "2. `10-kindle`、`豆瓣`、`英文` 与根目录主题区职责重叠，查找入口过多。",
        "3. 部分目录仍使用“新建文件夹”“用所选项目新建的文件夹”等临时名称。",
        "4. 文件名混有下载站标记、编号、副本标记、版本说明，影响排序与检索。",
        "5. AI 学习目录里夹着 Git 代码仓库、课程示例和数据集，不完全属于电子书。",
        "",
        "## 建议的整理顺序",
        "",
        "1. 先处理精确重复文件：每组保留一份，其余移入单独待删区，确认后再清理。",
        "2. 再处理同书不同格式：阅读优先保留 EPUB，扫描/图表/排版书保留 PDF，"
        "手机阅读需要时保留 MOBI/AZW3。",
        "3. 最后统一书架与文件名；不要在第一轮整理时同时移动和改名。",
        "4. AI 学习目录中的代码仓库单独归入“课程与代码”，不要混入电子书书架。",
        "",
        "## 建议目录",
        "",
        "```text",
        "<book-library-root>\\",
        "├─ 01_计算机",
        "├─ 02_文学",
        "├─ 03_网络文学",
        "├─ 04_人文与社会",
        "├─ 05_历史与传统文化",
        "├─ 06_商业与经济",
        "├─ 07_科学与自然",
        "├─ 08_哲学与心理",
        "├─ 08_艺术与摄影",
        "├─ 09_写作与表达",
        "├─ 10_语言学习",
        "├─ 11_外语读物",
        "├─ 12_教材与考试",
        "├─ 13_实用工具书",
        "├─ 90_综合与待归类",
        "├─ 91_课程与代码",
        "├─ 99_待确认重复",
        "└─ .book-library",
        "```",
        "",
        "## 本目录中的文件",
        "",
        "- `图书总目录.csv`：全部书刊与资料的完整索引，可用 Excel 筛选。",
        "- `书库总览.html`：可直接双击打开的搜索与浏览页面。",
        "- `精确重复文件.csv`：按 SHA-256 确认的完全相同文件。",
        "- `建议去重清单.csv`：每组标出建议保留的一份，其余仅列为“移出待确认”。",
        "- `疑似同名书目.csv`：同书不同格式、版本或命名方式。",
        "- `建议书架统计.csv`：各建议书架的规模。",
        "- `covers/`：离线封面缩略图，HTML 按相对路径引用。",
        "- `data/douban_ratings.json`：豆瓣评分匹配缓存。",
        "- `data/cover_manifest.json`：书籍与封面缩略图的对应关系。",
        "- `scripts/build_library_index.py`：书库变化后可重新生成索引。",
        "- `scripts/build_cover_cache.py`：重新提取 EPUB 封面。",
        "- `scripts/build_douban_cache.py`：重新生成豆瓣评分离线缓存。",
        "",
        "## 查重与删除",
        "",
        "- 重复标签来自 SHA-256 文件哈希校验，表示文件字节完全一致。",
        "- 点击“SHA重复”标签，会只显示该重复组内的全部文件，包括当前书籍。",
        "- “仅隐藏记录”只移动目录记录，不删除原始书籍；可用“恢复已隐藏记录”撤销。",
        "- 删除会把书籍移入 Windows 回收站，并同步移除数据库记录与书集成员。",
        "- 批量删除只处理当前明确勾选的书籍，执行前要求再次确认。",
        "",
        "## 文件夹变化刷新",
        "",
        "- 动态服务启动后，点击页面中的“刷新书库”才会扫描文件变化。",
        "- 已删除的书会从页面移除；被移动的书按“文件名 + 文件大小”匹配并更新路径。",
        "- 刷新后新增的文件只生成基础记录；封面、评分、精确哈希需重新运行索引脚本补齐。",
        "- 静态 HTML 没有后台服务，只适合查看，不会自动感知硬盘变化。",
        "",
        "## 风险提示",
        "",
        "- “疑似同名”只用于人工复核，不能仅凭书名删除。",
        "- 精确重复组也可能包含不同命名但内容相同的文件；清理前仍建议抽查。",
        "- 豆瓣评分来自公开数据快照，按规范化书名匹配；未匹配项显示“未获取”。",
        "- 豆瓣图书元数据与评分归豆瓣及原权利人所有，仅供个人离线索引使用。",
        "- 本轮没有执行任何移动或删除。",
        "",
    ]
    exports_dir = output_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    (exports_dir / "书库体检报告.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def generate_html(output_dir: Path, books: list[Book]) -> None:
    library_root = str(library_paths.ROOT)
    payload = []
    for book in books:
        payload.append(
            {
                "t": book.title,
                "a": book.author,
                "f": book.ext.upper(),
                "fl": book.filename_language,
                "l": infer_language(book.title, str(book.rel), book.language),
                "s": book.shelf,
                "ss": book.smart_subcategory,
                "c": book.category,
                "tags": [tag for tag in book.tags.split("；") if tag],
                "z": round(book.size / 1024 / 1024, 2),
                "sz": book.size,
                "d": "精确重复" if book.exact_group else "",
                "g": book.exact_group,
                "dc": book.exact_count,
                "q": "；".join(flag for flag in book.flags if flag != "作者未知"),
                "r": book.douban_rating,
                "rc": book.douban_count,
                "u": book.douban_url,
                "mt": book.douban_match_type,
                "cv": book.cover_file,
                "p": str(book.rel),
                "ap": str(book.path),
            }
        )
    payload.sort(key=lambda item: (item["s"], item["t"].casefold(), item["p"].casefold()))
    data_json = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    shelves = sorted({item["s"] for item in payload})
    subcategory_tree = {
        shelf: [
            [name, count]
            for name, count in Counter(
                item["ss"] for item in payload if item["s"] == shelf
            ).most_common()
        ]
        for shelf in shelves
    }
    subcategory_json = json.dumps(
        subcategory_tree, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    filename_languages = ["中文", "英文", "无法判断"]
    shelf_counts = Counter(item["s"] for item in payload)
    shelf_options = "".join(
        f'<option value="{html.escape(shelf)}">'
        f'{html.escape(shelf)} ({shelf_counts[shelf]:,})</option>'
        for shelf in shelves
    )
    language_options = "".join(
        f'<option value="{html.escape(language)}">{html.escape(language)}</option>'
        for language in filename_languages
    )
    primary_formats = {"EPUB", "PDF", "MOBI", "AZW3", "TXT"}
    format_tab_defs = [
        ("全部", ""),
        ("EPUB", "EPUB"),
        ("PDF", "PDF"),
        ("MOBI", "MOBI"),
        ("AZW3", "AZW3"),
        ("TXT", "TXT"),
        ("其他", "OTHER"),
    ]
    format_counts = Counter(item["f"] for item in payload)
    format_tab_html = "".join(
        f'<button class="format-tab{" active" if value == "EPUB" else ""}" '
        f'type="button" data-format="{value}">{label} '
        f'<span>{len(payload) if value == "" else (sum(count for fmt, count in format_counts.items() if fmt not in primary_formats) if value == "OTHER" else format_counts.get(value, 0)):,}</span></button>'
        for label, value in format_tab_defs
    )
    tag_counts = Counter(tag for item in payload for tag in item["tags"])
    tag_options = "".join(
        f'<option value="{html.escape(tag)}">'
        f'{html.escape(tag)} ({tag_counts[tag]:,})</option>'
        for tag in sorted(tag_counts, key=lambda value: (-tag_counts[value], value))
    )
    total_size = sum(book.size for book in books)
    duplicate_count = sum(1 for book in books if book.exact_group)
    rating_count = sum(1 for book in books if book.douban_rating is not None)
    cover_count = sum(1 for book in books if book.cover_file)
    title = "书库总览"
    document = rf"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script>
try {{
  const savedTheme = localStorage.getItem("book-library-theme");
  document.documentElement.dataset.theme = savedTheme === "dark" ||
    (!savedTheme && matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
}} catch (_) {{}}
</script>
<style>
:root {{
  --ink: #17201c;
  --muted: #66736d;
  --line: #d9dfdb;
  --paper: #f7f8f5;
  --panel: #ffffff;
  --accent: #0f6b4f;
  --accent-soft: #e8f3ee;
  --warn: #8a4b17;
  --surface-soft: #f1f4f1;
  --surface-strong: #e8efeb;
  --tag-soft: #fff3e7;
  --dup-soft: #fde9e5;
  --content-soft: #e9f3ee;
  --danger-soft: #fff0ed;
  --body-copy: #38443e;
  --warm-copy: #6b5a3c;
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --ink: #e6ece8;
  --muted: #9eaca5;
  --line: #33413a;
  --paper: #111714;
  --panel: #19211d;
  --accent: #69c59e;
  --accent-soft: #203a30;
  --warn: #f0b36b;
  --surface-soft: #202a25;
  --surface-strong: #29362f;
  --tag-soft: #382c20;
  --dup-soft: #3a2421;
  --content-soft: #20362d;
  --danger-soft: #3a2421;
  --body-copy: #cbd5cf;
  --warm-copy: #d8bd91;
}}
* {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
body {{
  margin: 0;
  color: var(--ink);
  background: var(--paper);
  font: 14px/1.5 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
}}
header {{
  padding: 22px clamp(16px, 4vw, 48px) 18px;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}}
.masthead {{
  display: flex;
  gap: 24px;
  align-items: end;
  justify-content: space-between;
  max-width: 1600px;
  margin: 0 auto;
}}
h1 {{
  margin: 0;
  font-size: 24px;
  line-height: 1.25;
  letter-spacing: 0;
}}
.summary {{
  margin-top: 5px;
  color: var(--muted);
}}
.masthead-tools {{
  display: flex;
  align-items: flex-end;
  gap: 14px;
}}
.theme-toggle {{
  flex: 0 0 auto;
  height: 34px;
  font-size: 12px;
}}
.stats {{
  display: flex;
  gap: 22px;
  flex-wrap: wrap;
  color: var(--muted);
}}
.stats strong {{
  display: block;
  color: var(--ink);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}}
.toolbar {{
  display: grid;
  grid-template-columns: minmax(220px, 2fr) repeat(5, minmax(105px, 1fr)) auto;
  gap: 10px;
  max-width: 1600px;
  margin: 18px auto 0;
}}
input, select, button {{
  height: 38px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--ink);
  padding: 0 11px;
  font: inherit;
}}
input:focus, select:focus, button:focus {{
  outline: 2px solid #9ac9b7;
  outline-offset: 1px;
}}
button {{
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
  vertical-align: middle;
}}
button:hover {{
  background: var(--accent-soft);
  border-color: #a9cbbd;
}}
.library-access {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  max-width: 1600px;
  margin: 10px auto 0;
  color: var(--muted);
  font-size: 12px;
}}
.classification-bar {{
  display: flex;
  align-items: flex-end;
  gap: 10px;
  max-width: 1600px;
  margin: 12px auto 0;
}}
.classification-bar label {{
  display: flex;
  min-width: min(320px, 42vw);
  flex-direction: column;
  gap: 4px;
  color: var(--muted);
  font-size: 11px;
}}
.classification-bar select,
.classification-bar input {{
  width: 100%;
}}
.classification-arrow {{
  padding-bottom: 9px;
  color: var(--muted);
  font-size: 18px;
}}
.collection-bar {{
  display: flex;
  align-items: flex-end;
  gap: 10px;
  max-width: 1600px;
  margin: 10px auto 0;
}}
.collection-list {{
  display: flex;
  min-width: min(420px, 52vw);
  flex: 1 1 520px;
  flex-direction: column;
  gap: 5px;
}}
.collection-label {{
  color: var(--muted);
  font-size: 11px;
}}
.collection-tabs {{
  display: flex;
  min-height: 38px;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}}
.collection-tab {{
  height: 32px;
  padding: 0 12px;
  background: var(--panel);
  color: var(--muted);
  font-size: 12px;
}}
.collection-tab:hover {{
  color: var(--ink);
}}
.collection-tab.active {{
  border-color: var(--accent);
  background: var(--accent);
  color: #fff;
}}
.collection-tab.active:hover {{
  background: var(--accent);
  color: #fff;
}}
.collection-create {{
  display: flex;
  flex: 1 1 320px;
  gap: 8px;
}}
.collection-create input {{
  min-width: 160px;
  flex: 1;
}}
.collection-create button {{
  flex: 0 0 auto;
}}
.library-access button {{
  height: 30px;
  font-size: 12px;
}}
.access-dot {{
  width: 8px;
  height: 8px;
  flex: 0 0 8px;
  border-radius: 50%;
  background: #b68a36;
}}
.access-dot.connected {{
  background: #1b7b58;
}}
.format-tabs {{
  display: flex;
  gap: 6px;
  max-width: 1600px;
  margin: 12px auto 0;
  overflow-x: auto;
  padding-bottom: 2px;
}}
.format-tab {{
  height: 34px;
  flex: 0 0 auto;
  border-radius: 6px;
  color: var(--muted);
}}
.format-tab span {{
  margin-left: 5px;
  color: inherit;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}}
.format-tab.active {{
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}}
.view-toggle {{
  display: inline-flex;
  flex: 0 0 auto;
  gap: 4px;
  margin-left: auto;
  padding-left: 8px;
}}
.view-button {{
  height: 34px;
  padding: 0 12px;
  color: var(--muted);
}}
.view-button.active {{
  border-color: var(--accent);
  background: var(--accent);
  color: white;
}}
.stats-tab {{
  margin-left: 0;
  border-color: #b7c9bf;
}}
.card-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 14px;
}}
.card-grid[hidden] {{
  display: none;
}}
.book-card {{
  display: flex;
  min-height: 410px;
  flex-direction: column;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}}
.card-cover-wrap {{
  position: relative;
  display: flex;
  height: 220px;
  align-items: center;
  justify-content: center;
  margin-bottom: 10px;
  background: var(--surface-soft);
  border-radius: 5px;
  overflow: hidden;
}}
.card-cover {{
  width: auto;
  max-width: 155px;
  height: 210px;
  object-fit: contain;
  cursor: pointer;
}}
.card-cover-placeholder {{
  display: flex;
  width: 145px;
  height: 205px;
  align-items: center;
  justify-content: center;
  border: 1px solid #d3dad6;
  border-radius: 4px;
  background: var(--surface-strong);
  color: var(--muted);
  font-weight: 600;
  cursor: pointer;
}}
.card-title {{
  display: -webkit-box;
  min-height: 44px;
  overflow: hidden;
  font-size: 15px;
  font-weight: 650;
  line-height: 1.45;
  cursor: pointer;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.card-author {{
  margin-top: 3px;
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.card-summary {{
  margin-top: 8px;
  color: var(--body-copy);
  font-size: 12px;
  line-height: 1.55;
}}
.card-reason {{
  margin-top: 6px;
  color: var(--warm-copy);
  font-size: 12px;
  line-height: 1.5;
}}
.list-ai {{
  margin-top: 7px;
  color: var(--body-copy);
  font-size: 12px;
  line-height: 1.5;
}}
.list-ai-reason {{
  margin-top: 3px;
  color: var(--warm-copy);
  font-size: 12px;
  line-height: 1.5;
}}
.card-tags {{
  margin-top: 8px;
}}
.card-douban {{
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
  color: #0b684c;
  font-size: 12px;
  font-weight: 650;
}}
.card-douban .rating-count {{
  display: inline;
  font-weight: 400;
}}
.card-size {{
  margin-left: auto;
  color: var(--muted);
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}}
.card-select {{
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 2;
  display: flex;
  width: 25px;
  height: 25px;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--panel);
}}
.card-actions {{
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: auto;
  padding-top: 10px;
}}
.card-actions button {{
  height: 27px;
  padding: 0 9px;
  font-size: 11px;
}}
.card-actions .ai-generate {{
  color: var(--accent);
  border-color: #afd0c0;
}}
.author-stats-panel {{
  width: min(760px, 100%);
}}
.author-stats-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}}
.author-stats-head h2 {{
  margin: 0;
}}
.author-stats-search {{
  width: 100%;
  margin-bottom: 10px;
}}
.author-stats-list {{
  max-height: min(65vh, 640px);
  overflow: auto;
  border-top: 1px solid var(--line);
}}
.author-stat-row {{
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 70px auto;
  gap: 12px;
  align-items: center;
  min-height: 42px;
  padding: 6px 4px;
  border-bottom: 1px solid #edf0ed;
}}
.author-stat-name {{
  overflow-wrap: anywhere;
}}
.author-stat-count {{
  color: var(--accent);
  font-weight: 700;
  text-align: right;
  font-variant-numeric: tabular-nums;
}}
.author-stat-row button {{
  height: 27px;
  padding: 0 9px;
  font-size: 11px;
}}
main {{
  max-width: 1600px;
  margin: 0 auto;
  padding: 18px clamp(10px, 3vw, 40px) 40px;
}}
.table-meta {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  min-height: 34px;
  color: var(--muted);
}}
.table-tools {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
.table-tools select {{
  width: auto;
  height: 30px;
  font-size: 12px;
}}
.selection-bar {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-height: 42px;
  margin: 0 0 9px;
  padding: 6px 10px;
  border: 1px solid #b7d5c8;
  border-radius: 7px;
  background: var(--accent-soft);
  color: var(--ink);
}}
.selection-bar[hidden] {{
  display: none;
}}
.selection-bar strong {{
  margin-right: 4px;
}}
.selection-bar select {{
  width: auto;
  min-width: 150px;
  height: 30px;
  font-size: 12px;
}}
.selection-bar button {{
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}}
.selection-bar .danger {{
  margin-left: auto;
}}
.active-group {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: 10px;
  color: var(--ink);
}}
.active-group button {{
  height: 25px;
  padding: 0 8px;
  font-size: 11px;
}}
.table-wrap {{
  overflow: auto;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  max-height: calc(100vh - 235px);
}}
table {{
  width: 100%;
  border-collapse: collapse;
  min-width: 1320px;
  table-layout: fixed;
}}
th, td {{
  padding: 8px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}}
th {{
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface-soft);
  font-weight: 600;
  white-space: nowrap;
}}
tbody tr:hover {{
  background: var(--accent-soft);
}}
td {{
  overflow-wrap: anywhere;
}}
.col-select {{ width: 3%; }}
.col-title {{ width: 22%; }}
.col-author {{ width: 11%; }}
.col-format {{ width: 5.5%; }}
.col-size {{ width: 6%; }}
.col-language {{ width: 6.5%; }}
.col-rating {{ width: 6.5%; }}
.col-shelf {{ width: 9%; }}
.col-detail {{ width: 19%; }}
.col-path {{ width: 11.5%; }}
.select-column {{
  width: 15px;
  height: 15px;
  padding: 0;
  accent-color: var(--accent);
}}
.bulk-selected {{
  background: var(--accent-soft);
}}
.file-size {{
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}}
.format {{
  display: inline-block;
  min-width: 43px;
  padding: 1px 6px;
  border: 1px solid var(--line);
  border-radius: 4px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}}
.book-cell {{
  display: flex;
  gap: 9px;
  align-items: flex-start;
}}
.cover {{
  width: 38px;
  height: 54px;
  flex: 0 0 38px;
  object-fit: cover;
  border: 1px solid #d3dad6;
  border-radius: 3px;
  background: #edf1ee;
}}
.cover-placeholder {{
  display: none;
  width: 38px;
  height: 54px;
  flex: 0 0 38px;
  align-items: center;
  justify-content: center;
  border: 1px solid #d3dad6;
  border-radius: 3px;
  background: #edf1ee;
  color: var(--muted);
  font-size: 10px;
  font-weight: 600;
}}
.book-title {{
  min-width: 0;
  padding-top: 1px;
  overflow-wrap: anywhere;
}}
.rating {{
  display: inline-block;
  color: #0b684c;
  font-size: 16px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-decoration: none;
}}
.rating:hover {{
  text-decoration: underline;
}}
.rating-count {{
  display: block;
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}}
.muted {{
  color: var(--muted);
}}
.copy-path {{
  height: 24px;
  margin-top: 0;
  padding: 0 8px;
  border-color: #c8d3cd;
  font-size: 11px;
}}
.toast {{
  position: fixed;
  left: 50%;
  bottom: 24px;
  z-index: 10;
  padding: 8px 14px;
  border-radius: 6px;
  background: #173c2f;
  color: white;
  opacity: 0;
  pointer-events: none;
  transform: translate(-50%, 8px);
  transition: opacity 160ms ease, transform 160ms ease;
}}
.toast.show {{
  opacity: 1;
  transform: translate(-50%, 0);
}}
.modal {{
  position: fixed;
  inset: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(15, 24, 20, 0.42);
}}
.modal[hidden] {{
  display: none;
}}
.modal-panel {{
  width: min(560px, 100%);
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: 0 18px 50px rgba(15, 24, 20, 0.2);
}}
.modal-panel h2 {{
  margin: 0 0 12px;
  font-size: 18px;
}}
.modal-book {{
  margin: 0 0 10px;
  font-weight: 600;
  overflow-wrap: anywhere;
}}
.modal-path {{
  max-height: 90px;
  overflow: auto;
  margin-bottom: 12px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--paper);
  color: var(--muted);
  font-size: 12px;
  overflow-wrap: anywhere;
}}
.delete-warning {{
  margin: 0 0 12px;
  padding: 9px 10px;
  border-left: 3px solid #a33a2b;
  background: var(--danger-soft);
  color: #6d2a20;
  font-size: 13px;
}}
.delete-confirm {{
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 16px;
  color: var(--ink);
  font-size: 13px;
}}
.delete-confirm input {{
  width: 16px;
  height: 16px;
  margin-top: 2px;
  padding: 0;
}}
.modal-actions {{
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}}
.danger {{
  border-color: #b94a39;
  background: #a83b2c;
  color: white;
}}
.danger:hover {{
  border-color: #933123;
  background: #933123;
  color: white;
}}
.danger:disabled {{
  cursor: not-allowed;
  opacity: 0.45;
}}
.douban-drawer {{
  position: fixed;
  top: 0;
  right: 0;
  z-index: 30;
  display: flex;
  width: min(560px, 100vw);
  height: 100vh;
  flex-direction: column;
  border-left: 1px solid var(--line);
  background: var(--panel);
  box-shadow: -12px 0 36px rgba(15, 24, 20, 0.2);
}}
.douban-drawer[hidden] {{
  display: none;
}}
.douban-drawer-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 58px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}}
.douban-drawer-head strong {{
  display: block;
}}
.douban-drawer-head span {{
  display: block;
  max-width: 300px;
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.douban-drawer-actions {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}}
.douban-drawer-actions a {{
  color: var(--accent);
  font-size: 12px;
}}
.douban-drawer-actions button {{
  height: 30px;
  padding: 0 10px;
}}
.douban-drawer iframe {{
  width: 100%;
  flex: 1;
  border: 0;
  background: var(--panel);
}}
.tag {{
  display: inline-block;
  margin: 0 4px 3px 0;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--tag-soft);
  color: var(--warn);
  font-size: 12px;
}}
.dup {{
  background: var(--dup-soft);
  color: #8b2d22;
}}
.content-tag {{
  background: var(--content-soft);
  color: #205d47;
}}
.dup-link {{
  border: 0;
  cursor: pointer;
  font-family: inherit;
}}
.dup-link:hover {{
  background: #f8d7d1;
}}
.path {{
  color: var(--muted);
  font-size: 12px;
  word-break: break-all;
}}
.path-actions {{
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  align-items: center;
  margin-top: 5px;
}}
.path-actions button {{
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}}
.path-actions .delete-open {{
  border-color: #ddb9b2;
  color: #8b2d22;
}}
.path-actions .delete-open:hover {{
  background: var(--danger-soft);
  border-color: #d69c91;
}}
.pager {{
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}}
.pager button {{
  min-width: 82px;
}}
.empty {{
  padding: 36px;
  text-align: center;
  color: var(--muted);
}}
@media (max-width: 900px) {{
  .masthead {{ align-items: start; flex-direction: column; }}
  .masthead-tools {{ width: 100%; justify-content: space-between; }}
  .toolbar {{ grid-template-columns: 1fr 1fr; }}
  .toolbar input {{ grid-column: 1 / -1; }}
  .table-wrap {{ max-height: none; }}
}}
@media (max-width: 560px) {{
  .toolbar {{ grid-template-columns: 1fr; }}
  .toolbar input {{ grid-column: auto; }}
  .stats {{ gap: 12px; }}
  .classification-bar {{
    align-items: stretch;
    flex-direction: column;
  }}
  .classification-bar label {{ min-width: 0; }}
  .classification-arrow {{ display: none; }}
  .collection-bar,
  .collection-create {{
    align-items: stretch;
    flex-direction: column;
  }}
  .collection-list {{ min-width: 0; }}
  .selection-bar .danger {{ margin-left: 0; }}
}}
</style>
</head>
<body>
<header>
  <div class="masthead">
    <div>
      <h1>{title}</h1>
      <div class="summary">按建议书架排序；搜索可匹配书名、作者、路径、分类和标记。</div>
    </div>
    <div class="masthead-tools">
      <div class="stats">
        <span><strong id="stat-files">{len(books):,}</strong>书刊/资料</span>
        <span><strong id="stat-size">{format_bytes(total_size)}</strong>总大小</span>
        <span><strong id="stat-covers">{cover_count:,}</strong>本地封面</span>
        <span><strong id="stat-ratings">{rating_count:,}</strong>豆瓣评分</span>
        <span><strong id="stat-duplicates">{duplicate_count:,}</strong>精确重复成员</span>
        <span><strong id="stat-shelves">{len(shelves)}</strong>建议书架</span>
      </div>
      <button id="theme-toggle" class="theme-toggle" type="button">深色</button>
    </div>
  </div>
  <div class="toolbar">
    <input id="search" type="search" placeholder="搜索书名、作者、路径或分类">
    <select id="language"><option value="">全部文件名语言</option>{language_options}</select>
    <input id="author" type="search" placeholder="直接输入作者名">
    <select id="tag"><option value="">全部标签</option>{tag_options}</select>
    <select id="rating">
      <option value="">全部评分</option>
      <option value="9">9 分及以上</option>
      <option value="8">8 分及以上</option>
      <option value="rated">仅已获取评分</option>
      <option value="unrated">仅未获取评分</option>
    </select>
    <select id="duplicate">
      <option value="">全部记录</option>
      <option value="yes">仅精确重复</option>
      <option value="no">排除精确重复</option>
    </select>
    <button id="reset" type="button">清除筛选</button>
  </div>
  <div class="classification-bar">
    <label>
      <span>一级分类</span>
      <select id="shelf"><option value="">全部分类</option>{shelf_options}</select>
    </label>
    <span class="classification-arrow" aria-hidden="true">→</span>
    <label>
      <span>二级分类</span>
      <select id="smart-subcategory"><option value="">全部二级分类</option></select>
    </label>
  </div>
  <div class="collection-bar">
    <div class="collection-list">
      <span class="collection-label">书集</span>
      <div id="collection-tabs" class="collection-tabs" role="group" aria-label="按书集筛选">
        <button class="collection-tab active" type="button" data-collection="" aria-pressed="true">全部书集</button>
      </div>
    </div>
    <div class="collection-create">
      <input id="collection-name" type="text" maxlength="80" placeholder="输入新书集名称">
      <button id="create-collection" type="button">创建书集</button>
    </div>
  </div>
  <div class="library-access">
    <span id="access-dot" class="access-dot"></span>
    <span id="access-status">删除书籍前需要授权书库根目录</span>
    <button id="connect-library" type="button">连接书库文件夹</button>
    <button id="sync-library" type="button">刷新书库</button>
    <span id="sync-status">等待手动刷新</span>
    <button id="refresh-ai" type="button">刷新AI简介</button>
    <button id="restore-hidden" type="button" hidden>恢复已隐藏记录</button>
  </div>
  <div class="format-tabs" role="tablist" aria-label="按格式筛选">
    {format_tab_html}
    <div class="view-toggle" role="group" aria-label="视图切换">
      <button id="list-view" class="view-button active" type="button">列表</button>
      <button id="card-view" class="view-button" type="button">卡片</button>
    </div>
    <button id="author-stats-open" class="format-tab stats-tab" type="button">作者统计</button>
  </div>
</header>
<main>
  <div class="table-meta">
    <span>
      <span id="count"></span>
      <span id="active-group" class="active-group" hidden>
        <span id="active-group-text"></span>
        <button id="clear-group" type="button">清除重复组筛选</button>
      </span>
    </span>
    <div class="table-tools">
      <label for="size-sort">排序</label>
      <select id="size-sort">
        <option value="">默认排序</option>
        <option value="size-desc">文件大小：大到小</option>
        <option value="size-asc">文件大小：小到大</option>
      </select>
      <span>每页 100 条</span>
    </div>
  </div>
  <div id="selection-bar" class="selection-bar" hidden>
    <strong>已选 <span id="selection-count">0</span> 本</strong>
    <button id="select-page" type="button">选择本页</button>
    <select id="bulk-collection"><option value="">选择书集</option></select>
    <button id="bulk-add" type="button">加入书集</button>
    <button id="bulk-clear" type="button">取消选择</button>
    <button id="bulk-delete" class="danger" type="button">删除所选</button>
  </div>
  <div id="table-wrap" class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="col-select"><input id="select-visible" class="select-column" type="checkbox" aria-label="选择本页"></th>
          <th class="col-title">书名</th>
          <th class="col-author">作者</th>
          <th class="col-format">格式</th>
          <th class="col-size">大小</th>
          <th class="col-language">文件名语言</th>
          <th class="col-rating">豆瓣评分</th>
          <th class="col-shelf">建议书架</th>
          <th class="col-detail">分类 / 标记</th>
          <th class="col-path">相对路径</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
    <div id="empty" class="empty" hidden>没有符合当前条件的记录。</div>
  </div>
  <div id="card-grid" class="card-grid" hidden></div>
  <div class="pager">
    <button id="prev" type="button">上一页</button>
    <span id="page"></span>
    <button id="next" type="button">下一页</button>
  </div>
</main>
<aside id="douban-drawer" class="douban-drawer" hidden>
  <div class="douban-drawer-head">
    <div>
      <strong>豆瓣搜书</strong>
      <span id="douban-drawer-title"></span>
    </div>
    <div class="douban-drawer-actions">
      <a id="douban-new-window" href="#" target="_blank" rel="noopener">新窗口打开</a>
      <button id="douban-drawer-close" type="button">关闭</button>
    </div>
  </div>
  <iframe id="douban-frame" title="豆瓣搜书" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer"></iframe>
</aside>
<div id="author-stats-modal" class="modal" hidden>
  <div class="modal-panel author-stats-panel" role="dialog" aria-modal="true" aria-labelledby="author-stats-title">
    <div class="author-stats-head">
      <h2 id="author-stats-title">作者书籍统计</h2>
      <button id="author-stats-close" type="button">关闭</button>
    </div>
    <input id="author-stats-search" class="author-stats-search" type="search" placeholder="搜索作者">
    <div id="author-stats-summary" class="muted"></div>
    <div id="author-stats-list" class="author-stats-list"></div>
  </div>
</div>
<div id="delete-modal" class="modal" hidden>
  <div class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="delete-modal-title">
    <h2 id="delete-modal-title">删除确认</h2>
    <p id="delete-modal-book" class="modal-book"></p>
    <div id="delete-modal-path" class="modal-path"></div>
    <p id="delete-warning" class="delete-warning">
      删除会把文件移入 Windows 回收站，并同时从书库记录中移除。
      如只想让此条记录从页面消失，请选择“仅隐藏记录”。
    </p>
    <label class="delete-confirm">
      <input id="delete-ack" type="checkbox">
      <span id="delete-ack-text">我确认将所选书籍移入 Windows 回收站</span>
    </label>
    <div class="modal-actions">
      <button id="cancel-delete" type="button">取消</button>
      <button id="hide-record" type="button">仅隐藏记录</button>
      <button id="confirm-delete" class="danger" type="button" disabled>移入回收站</button>
    </div>
  </div>
</div>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script>
const LIBRARY_ROOT = {json.dumps(library_root, ensure_ascii=False)};
const BOOK_EXTENSIONS = new Set([
  "epub", "pdf", "mobi", "azw", "azw3", "djvu", "uvz",
  "cbz", "cbr", "chm", "doc", "docx", "fb2", "html", "lit",
  "odt", "pdb", "ppt", "pptx", "rtf", "txt", "xls", "xlsx"
]);
let SERVER_MODE = false;
const BRIDGE_URL = "http://127.0.0.1:18767";
let BRIDGE_MODE = false;
let AI_SUMMARIES = new Map();
let AI_BATCH_PROGRESS = null;
let coverClickTimer = 0;
const SUB_CATEGORY_COUNTS = {subcategory_json};
let RAW_DATA = {data_json};
const HIDDEN_RECORD_KEY = "book-library-hidden-records-v1";
const DELETED_FILE_KEY = "book-library-deleted-files-v1";
const HANDLE_DB = "book-library-handles-v1";
const HANDLE_STORE = "handles";
let hiddenRecordPaths = loadPathSet(HIDDEN_RECORD_KEY);
let deletedFilePaths = loadPathSet(DELETED_FILE_KEY);
let DATA = RAW_DATA.filter(
  (item) => !hiddenRecordPaths.has(item.ap) && !deletedFilePaths.has(item.ap)
);
let groupCounts = new Map();
const state = {{
  page: 1,
  pageSize: 100,
  filtered: DATA,
  duplicateGroup: "",
  formatTab: "EPUB",
  smartSubcategory: "",
  viewMode: "list",
  sort: "",
  collectionId: ""
}};
let COLLECTIONS = [];
let selectedPaths = new Set();
let visiblePagePaths = [];
const byId = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({{
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}}[ch]));

function loadPathSet(key) {{
  try {{
    return new Set(JSON.parse(localStorage.getItem(key) || "[]"));
  }} catch {{
    return new Set();
  }}
}}

function savePathSet(key, values) {{
  try {{
    localStorage.setItem(key, JSON.stringify([...values]));
  }} catch {{
    // Deleting still works for the current tab when storage is unavailable.
  }}
}}

function rebuildGroupCounts() {{
  groupCounts = new Map();
  for (const item of DATA) {{
    if (item.g) groupCounts.set(item.g, (groupCounts.get(item.g) || 0) + 1);
  }}
}}

function formatLibrarySize(totalMB) {{
  if (totalMB >= 1024) return `${{(totalMB / 1024).toFixed(2)}} GB`;
  return `${{totalMB.toFixed(1)}} MB`;
}}

function formatFileSize(bytes) {{
  const value = Number(bytes || 0);
  if (value >= 1024 ** 3) return `${{(value / 1024 ** 3).toFixed(2)}} GB`;
  if (value >= 1024 ** 2) return `${{(value / 1024 ** 2).toFixed(2)}} MB`;
  if (value >= 1024) return `${{(value / 1024).toFixed(1)}} KB`;
  return `${{value}} B`;
}}

function updateSelectionUi() {{
  const count = selectedPaths.size;
  byId("selection-bar").hidden = count === 0;
  byId("selection-count").textContent = count.toLocaleString();
  const pageSelected = visiblePagePaths.length > 0 &&
    visiblePagePaths.every((path) => selectedPaths.has(path));
  const pagePartiallySelected = visiblePagePaths.some((path) => selectedPaths.has(path));
  byId("select-visible").checked = pageSelected;
  byId("select-visible").indeterminate = pagePartiallySelected && !pageSelected;
  byId("select-page").textContent = pageSelected ? "取消本页" : "选择本页";
  byId("bulk-add").disabled = count === 0 || COLLECTIONS.length === 0;
}}

function selectCurrentPage(select) {{
  for (const path of visiblePagePaths) {{
    if (select) selectedPaths.add(path);
    else selectedPaths.delete(path);
  }}
  render();
}}

function updateThemeToggle() {{
  const dark = document.documentElement.dataset.theme === "dark";
  byId("theme-toggle").textContent = dark ? "浅色" : "深色";
  byId("theme-toggle").setAttribute("aria-pressed", String(dark));
}}

function toggleTheme() {{
  const dark = document.documentElement.dataset.theme !== "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {{ localStorage.setItem("book-library-theme", dark ? "dark" : "light"); }} catch {{}}
  updateThemeToggle();
}}

function pathKey(value) {{
  return String(value || "").replace(/\\/g, "/").toLocaleLowerCase();
}}

function collectionApiPath(path) {{
  if (BRIDGE_MODE) return `${{BRIDGE_URL}}${{path}}`;
  if (SERVER_MODE) return path;
  return "";
}}

function refreshCollectionControls() {{
  const tabs = byId("collection-tabs");
  const bulk = byId("bulk-collection");
  const previousFilter = state.collectionId;
  const previousBulk = bulk.value;
  const options = COLLECTIONS.map(
    (item) => `<option value="${{item.id}}">${{esc(item.name)}} (${{item.paths.length.toLocaleString()}})</option>`
  ).join("");
  bulk.innerHTML = '<option value="">选择书集</option>' + options;
  bulk.value = COLLECTIONS.some((item) => String(item.id) === previousBulk) ? previousBulk : "";
  state.collectionId = COLLECTIONS.some((item) => String(item.id) === previousFilter) ? previousFilter : "";
  const tabItems = [
    {{ id: "", name: "全部书集", count: null }},
    ...COLLECTIONS.map((item) => ({{ id: String(item.id), name: item.name, count: item.paths.length }}))
  ];
  tabs.innerHTML = tabItems.map((item) => {{
    const active = String(item.id) === state.collectionId;
    const count = item.count === null ? "" : ` (${{item.count.toLocaleString()}})`;
    return `<button class="collection-tab${{active ? " active" : ""}}" type="button"
              data-collection="${{esc(item.id)}}" aria-pressed="${{active}}">${{esc(item.name)}}${{count}}</button>`;
  }}).join("");
}}

async function loadCollections() {{
  const endpoint = collectionApiPath("/api/collections");
  if (!endpoint) {{
    COLLECTIONS = [];
    refreshCollectionControls();
    return false;
  }}
  try {{
    const response = await fetch(endpoint, {{ cache: "no-store" }});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "书集读取失败");
    COLLECTIONS = Array.isArray(payload.items) ? payload.items : [];
    refreshCollectionControls();
    return true;
  }} catch (error) {{
    COLLECTIONS = [];
    refreshCollectionControls();
    if (BRIDGE_MODE) showToast(`书集读取失败：${{error.message}}`);
    return false;
  }}
}}

async function createCollection() {{
  const input = byId("collection-name");
  const name = input.value.trim();
  if (!name) {{
    showToast("请输入书集名称");
    input.focus();
    return;
  }}
  const endpoint = collectionApiPath("/api/collections");
  if (!endpoint) {{
    showToast("请先启动本地服务");
    return;
  }}
  try {{
    const response = await fetch(endpoint, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ name }})
    }});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "创建失败");
    input.value = "";
    await loadCollections();
    byId("bulk-collection").value = String(payload.item.id);
    applyFilters();
    showToast(`书集“${{payload.item.name}}”已创建`);
  }} catch (error) {{
    showToast(`创建失败：${{error.message}}`);
  }}
}}

async function addSelectedToCollection() {{
  const collectionId = Number(byId("bulk-collection").value || 0);
  if (!collectionId) {{
    showToast("请先选择书集");
    return;
  }}
  if (!selectedPaths.size) {{
    showToast("请先选择书籍");
    return;
  }}
  const endpoint = collectionApiPath("/api/collections/members");
  if (!endpoint) {{
    showToast("请先启动本地服务");
    return;
  }}
  try {{
    const response = await fetch(endpoint, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ collection_id: collectionId, paths: [...selectedPaths] }})
    }});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "加入失败");
    await loadCollections();
    applyFilters();
    const suffix = payload.missing ? `，${{payload.missing}} 本未找到` : "";
    showToast(`已加入 ${{payload.added}} 本${{suffix}}`);
  }} catch (error) {{
    showToast(`加入失败：${{error.message}}`);
  }}
}}

function fileBasename(value) {{
  return String(value || "").split(/[\\\\/]/).pop() || "";
}}

function absoluteLibraryPath(relativePath) {{
  return `${{LIBRARY_ROOT}}\\${{String(relativePath).replace(/[\\\\/]+/g, "\\")}}`;
}}

function filenameLanguage(name) {{
  const stem = String(name || "").replace(/\.[^.]+$/, "");
  if (/[\u3400-\u9fff]/.test(stem)) return "中文";
  if (/[A-Za-z]{{3,}}/.test(stem)) return "英文";
  return "无法判断";
}}

function titleFromFilename(name) {{
  let title = String(name || "").replace(/\.[^.]+$/, "");
  title = title.replace(/\s*[-_ ](?:副本|copy)\s*\d*\s*$/i, "");
  title = title.replace(/\s*[\[(（【]\s*(?:Z-Library|扫描版?|高清版?|电子版?)\s*[\])）】]\s*/gi, "");
  return title.trim() || name;
}}

function categoryFromPath(relativePath) {{
  const parts = String(relativePath).split(/[\\\\/]+/).filter(Boolean);
  if (!parts.length) return "";
  if (parts[0] === "【00总文库】") return parts[1] || parts[0];
  return parts[1] || parts[0];
}}

function inferShelf(relativePath, title) {{
  const text = `${{relativePath}} ${{title}}`.toLocaleLowerCase();
  const rules = [
    ["计算机", ["ai学习", "人工智能", "机器学习", "深度学习", "大模型", "llm", "langchain", "神经网络", "python", "数据编程", "1-互联网", "算法", "计算机", "编程"]],
    ["网络文学", ["7-网文", "网文", "晋江", "网络文学"]],
    ["写作与表达", ["写作", "描写辞典", "故事写作", "文案"]],
    ["艺术与摄影", ["摄影", "5-艺术", "书法", "绘画", "名画", "篆刻"]],
    ["语言学习", ["9-英语", "英语学习", "英语语法", "英语词汇", "英语口语", "雅思", "托福"]],
    ["历史与传统文化", ["国学", "历史", "资治通鉴", "史记", "汉书", "中国史", "世界史", "民国", "帝国", "王朝", "二战", "一战", "传记", "古籍"]],
    ["商业与经济", ["商业", "创业", "证券", "理财", "金融", "经济", "政经", "投资", "资本", "货币", "市场", "企业", "管理", "营销", "领导力", "财富"]],
    ["科学与自然", ["科学", "科普", "数学", "物理", "化学", "生物", "基因", "进化", "量子", "宇宙", "医学", "健康"]],
    ["哲学与心理", ["哲思", "哲理", "哲学", "心理", "认知", "逻辑", "伦理"]],
    ["文学", ["文学", "小说", "名著", "诗歌", "散文", "随笔", "文集", "全集", "茅盾文学奖"]],
    ["外语读物", ["英文", "9-英语", "英文原版", "双语"]],
    ["教材与考试", ["教材", "教程", "考试", "考研", "习题", "高等数学", "概率论", "线性代数"]],
    ["人文与社会", ["社科", "政治", "社会学", "文化", "法律", "思想", "汉译世界学术名著"]]
  ];
  for (const [shelf, keywords] of rules) {{
    if (keywords.some((keyword) => text.includes(keyword))) return shelf;
  }}
  return "综合与待归类";
}}

function newBookFromPath(relativePath, size) {{
  const name = fileBasename(relativePath);
  const format = (name.split(".").pop() || "").toUpperCase();
  const title = titleFromFilename(name);
  const language = filenameLanguage(name);
  return {{
    t: title,
    a: "",
    f: format,
    fl: language,
    l: language === "无法判断" ? "未知" : language,
    s: inferShelf(relativePath, title),
    c: categoryFromPath(relativePath),
    z: Math.round(size / 1024 / 1024 * 100) / 100,
    sz: size,
    d: "",
    g: "",
    dc: 0,
    q: "手动刷新新增；重建索引可补封面、评分和哈希",
    r: null,
    rc: null,
    u: "",
    mt: "",
    cv: "",
    p: relativePath,
    ap: absoluteLibraryPath(relativePath),
  }};
}}

async function walkLibraryFiles(directory, prefix = "", result = []) {{
  const ignoredDirectories = new Set([
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules", ".book-library", "_书库整理"
  ]);
  for await (const [name, handle] of directory.entries()) {{
    if (name.startsWith("._")) continue;
    const relativePath = prefix ? `${{prefix}}\\${{name}}` : name;
    if (handle.kind === "directory") {{
      if (!ignoredDirectories.has(name.toLocaleLowerCase())) {{
        await walkLibraryFiles(handle, relativePath, result);
      }}
      continue;
    }}
    const extension = (name.split(".").pop() || "").toLocaleLowerCase();
    if (!BOOK_EXTENSIONS.has(extension)) continue;
    if (["desktop.ini", "thumbs.db", ".ds_store"].includes(name.toLocaleLowerCase())) continue;
    const file = await handle.getFile();
    result.push({{
      relativePath,
      name,
      size: file.size,
      lastModified: file.lastModified,
    }});
  }}
  return result;
}}

let isSyncing = false;

async function syncLibrary({{ interactive = false, silent = true }} = {{}}) {{
  if (isSyncing) return null;
  if (SERVER_MODE) {{
    if (!interactive) {{
      await pollServerStatus(false);
      return null;
    }}
    isSyncing = true;
    byId("sync-status").textContent = "正在请求书库同步…";
    try {{
      const response = await fetch("/api/rescan", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: "{{}}"
      }});
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "同步失败");
      serverVersion = payload.version;
      await pollServerStatus(false);
      showToast("书库已刷新，正在重新载入页面");
      window.setTimeout(() => location.reload(), 220);
      return payload;
    }} catch (error) {{
      byId("sync-status").textContent = `同步失败：${{error.message}}`;
      showToast(`同步失败：${{error.message}}`);
      return null;
    }} finally {{
      isSyncing = false;
    }}
  }}
  if (!libraryRootHandle) {{
    if (!interactive) return null;
    const connected = await connectLibrary();
    if (!connected) return null;
  }}
  const permission = await libraryRootHandle.queryPermission({{ mode: "readwrite" }});
  if (permission !== "granted" && !interactive) return null;
  if (permission !== "granted") {{
    const requested = await libraryRootHandle.requestPermission({{ mode: "readwrite" }});
    if (requested !== "granted") return null;
  }}

  isSyncing = true;
  byId("sync-status").textContent = "正在扫描书库…";
  try {{
    const scanned = await walkLibraryFiles(libraryRootHandle);
    const oldRaw = RAW_DATA.slice();
    const oldByPath = new Map(oldRaw.map((item) => [pathKey(item.p), item]));
    const scannedKeys = new Set(scanned.map((file) => pathKey(file.relativePath)));
    const missingByKey = new Map();
    for (const item of oldRaw) {{
      if (scannedKeys.has(pathKey(item.p))) continue;
      const key = `${{fileBasename(item.p).toLocaleLowerCase()}}|${{Number(item.z).toFixed(2)}}`;
      if (!missingByKey.has(key)) missingByKey.set(key, []);
      missingByKey.get(key).push(item);
    }}

    const nextRaw = [];
    const used = new Set();
    let newCount = 0;
    let matchedCount = 0;
    for (const file of scanned) {{
      const sizeMB = Math.round(file.size / 1024 / 1024 * 100) / 100;
      let item = oldByPath.get(pathKey(file.relativePath));
      if (item) {{
        item.z = sizeMB;
        item.sz = file.size;
        matchedCount += 1;
      }} else {{
        const key = `${{file.name.toLocaleLowerCase()}}|${{sizeMB.toFixed(2)}}`;
        const candidates = (missingByKey.get(key) || []).filter((candidate) => !used.has(candidate.ap));
        if (candidates.length === 1) {{
          item = candidates[0];
          item.p = file.relativePath;
          item.ap = absoluteLibraryPath(file.relativePath);
          item.z = sizeMB;
          item.sz = file.size;
          item.s = inferShelf(file.relativePath, item.t);
          item.c = categoryFromPath(file.relativePath);
          matchedCount += 1;
        }} else {{
          item = newBookFromPath(file.relativePath, file.size);
          newCount += 1;
        }}
      }}
      used.add(item.ap);
      nextRaw.push(item);
    }}

    const removedCount = Math.max(0, oldRaw.length - matchedCount);
    const nextPaths = new Set(nextRaw.map((item) => item.ap));
    hiddenRecordPaths = new Set([...hiddenRecordPaths].filter((path) => nextPaths.has(path)));
    deletedFilePaths = new Set([...deletedFilePaths].filter((path) => nextPaths.has(path)));
    savePathSet(HIDDEN_RECORD_KEY, hiddenRecordPaths);
    savePathSet(DELETED_FILE_KEY, deletedFilePaths);

    RAW_DATA = nextRaw.sort((left, right) =>
      `${{left.s}}\0${{left.t}}\0${{left.p}}`.localeCompare(
        `${{right.s}}\0${{right.t}}\0${{right.p}}`, "zh-CN"
      )
    );
    DATA = RAW_DATA.filter(
      (item) => !hiddenRecordPaths.has(item.ap) && !deletedFilePaths.has(item.ap)
    );
    rebuildGroupCounts();
    applyFilters();
    updateSummary();
    const stamp = new Date().toLocaleTimeString("zh-CN", {{ hour12: false }});
    byId("sync-status").textContent =
      `${{silent ? "自动同步" : "同步完成"}} ${{stamp}} · 新增 ${{newCount}} · 移除 ${{removedCount}}`;
    if (!silent) {{
      showToast(`书库同步完成：新增 ${{newCount}}，移除 ${{removedCount}}`);
    }}
    return {{ newCount, removedCount, total: nextRaw.length }};
  }} catch (error) {{
    byId("sync-status").textContent = `同步失败：${{error.message}}`;
    if (!silent) showToast(`同步失败：${{error.message}}`);
    return null;
  }} finally {{
    isSyncing = false;
  }}
}}

function updateSummary() {{
  byId("stat-files").textContent = DATA.length.toLocaleString();
  byId("stat-size").textContent = formatLibrarySize(
    DATA.reduce((total, item) => total + item.z, 0)
  );
  byId("stat-covers").textContent = DATA.filter((item) => item.cv).length.toLocaleString();
  byId("stat-ratings").textContent = DATA.filter((item) => item.r !== null).length.toLocaleString();
  byId("stat-duplicates").textContent = DATA.filter((item) => item.d).length.toLocaleString();
  byId("stat-shelves").textContent = new Set(DATA.map((item) => item.s)).size.toLocaleString();
  byId("restore-hidden").hidden = hiddenRecordPaths.size === 0;
  byId("restore-hidden").textContent = `恢复已隐藏记录 (${{hiddenRecordPaths.size.toLocaleString()}})`;
  updateFormatTabs();
}}
const compactVotes = new Intl.NumberFormat("zh-CN", {{
  notation: "compact",
  maximumFractionDigits: 1
}});

function formatVotes(count) {{
  if (count === null || count === undefined) return "评分人数未知";
  return `${{compactVotes.format(count)}}人评价`;
}}

let toastTimer = 0;
function showToast(message) {{
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("show"), 1800);
}}

async function copyPath(text) {{
  if (navigator.clipboard?.writeText) {{
    try {{
      await navigator.clipboard.writeText(text);
      return;
    }} catch {{
      // Fall through to the selection-based fallback for file:// pages.
    }}
  }}
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.focus();
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  if (!copied) throw new Error("copy failed");
}}

async function openBook(relativePath) {{
  const endpoint = SERVER_MODE
    ? "/api/open"
    : (BRIDGE_MODE ? `${{BRIDGE_URL}}/api/open` : "");
  if (!endpoint) {{
    throw new Error("本地打开桥接服务未运行");
  }}
  const response = await fetch(endpoint, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ path: relativePath }})
  }});
  const payload = await response.json();
  if (!response.ok || !payload.ok) {{
    throw new Error(payload.error || "打开失败");
  }}
}}

async function revealBook(relativePath) {{
  if (window.LocalDock?.reveal) {{
    await window.LocalDock.reveal(relativePath);
    return;
  }}
  let endpoint = "";
  if (SERVER_MODE) endpoint = "/__localdock/api/reveal";
  else if (BRIDGE_MODE) endpoint = `${{BRIDGE_URL}}/api/reveal`;
  if (!endpoint) throw new Error("本地文件定位服务未运行");
  const response = await fetch(endpoint, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ path: relativePath }})
  }});
  const payload = await response.json();
  if (!response.ok || !payload.ok) {{
    throw new Error(payload.error || "定位失败");
  }}
}}

function doubanSearchUrl(title) {{
  return "https://search.douban.com/book/subject_search?search_text=" +
    encodeURIComponent(title || "") + "&cat=1001";
}}

function openDoubanDrawer(title) {{
  const url = doubanSearchUrl(title);
  byId("douban-drawer-title").textContent = title || "";
  byId("douban-new-window").href = url;
  byId("douban-frame").src = url;
  byId("douban-drawer").hidden = false;
}}

function closeDoubanDrawer() {{
  byId("douban-drawer").hidden = true;
  byId("douban-frame").src = "about:blank";
}}

function searchable(item) {{
  return [item.t, item.a, item.fl, item.l, item.s, item.ss, item.c, item.q, item.p, ...(item.tags || [])]
    .join(" ").toLocaleLowerCase();
}}

function duplicateTag(item) {{
  const count = groupCounts.get(item.g) || 0;
  if (count <= 1) return "";
  return `<button class="tag dup dup-link" type="button" data-group="${{esc(item.g)}}"
            title="SHA-256 校验为字节完全一致，共 ${{count}} 个文件">SHA重复 ×${{count}}</button>`;
}}

function aiSummaryFor(item) {{
  return AI_SUMMARIES.get(aiKey(item));
}}

function renderCards(items) {{
  if (!items.length) {{
    byId("card-grid").innerHTML = '<div class="empty">没有符合当前条件的记录。</div>';
    return;
  }}
  byId("card-grid").innerHTML = items.map((item) => {{
    const ai = aiSummaryFor(item);
    const aiAvailable = item.f !== "PDF";
    const cover = item.cv
      ? `<img class="card-cover" src="${{esc(item.cv)}}" alt="${{esc(item.t)}}" loading="lazy"
             data-open-path="${{esc(item.p)}}" data-douban-title="${{esc(item.t)}}"
             title="单击查看豆瓣，双击用默认软件打开">`
      : `<div class="card-cover-placeholder" data-open-path="${{esc(item.p)}}"
             data-douban-title="${{esc(item.t)}}"
             title="单击查看豆瓣，双击用默认软件打开">${{esc(item.f)}}</div>`;
    const canOpen = SERVER_MODE || BRIDGE_MODE;
    return `
      <article class="book-card${{selectedPaths.has(item.p) ? " bulk-selected" : ""}}">
        <div class="card-cover-wrap">
          <label class="card-select" title="选择此书">
            <input class="select-column select-book" type="checkbox" data-path="${{esc(item.p)}}"
                   ${{selectedPaths.has(item.p) ? "checked" : ""}} aria-label="选择《${{esc(item.t)}}》">
          </label>
          ${{cover}}
        </div>
        <div class="card-title" title="${{esc(item.t)}}" data-open-path="${{esc(item.p)}}">${{esc(item.t)}}</div>
        <div class="card-author">${{esc(item.a || "作者未知")}}</div>
        <div class="card-douban">
          <span>豆瓣</span>
          ${{
            item.r !== null
              ? `<a class="rating" href="${{esc(item.u)}}" target="_blank" rel="noopener">${{Number(item.r).toFixed(1)}}</a>
                 <span class="rating-count">${{formatVotes(item.rc)}}</span>`
              : '<span class="muted">未获取</span>'
          }}
          <span class="card-size">${{formatFileSize(item.sz)}}</span>
        </div>
        <div class="card-tags">
          <span class="tag content-tag">${{esc(item.s)}}</span>
          <span class="tag content-tag">${{esc(item.ss)}}</span>
          ${{(item.tags || []).slice(0, 4).map((tag) => `<span class="tag">${{esc(tag)}}</span>`).join("")}}
        </div>
        ${{
          aiAvailable
            ? `<div class="card-summary">${{ai ? `<strong>简介：</strong>${{esc(ai.summary)}}` : "AI 简介尚未生成"}}</div>
               <div class="card-reason">${{ai ? `<strong>推荐：</strong>${{esc(ai.reason)}}` : ""}}</div>`
            : ""
        }}
        <div class="card-actions">
          ${{canOpen ? `<button class="open-book" type="button" data-path="${{esc(item.p)}}">打开</button>` : ""}}
          <button class="reveal-book" type="button" data-path="${{esc(item.p)}}">定位</button>
          <button class="douban-search" type="button" data-title="${{esc(item.t)}}">豆瓣</button>
          ${{BRIDGE_MODE && aiAvailable && !ai ? `<button class="ai-generate" type="button" data-path="${{esc(item.p)}}">生成AI简介</button>` : ""}}
          <button class="copy-path" type="button" data-path="${{esc(item.ap)}}">复制路径</button>
          <button class="delete-open" type="button" data-path="${{esc(item.ap)}}">删除…</button>
        </div>
      </article>
    `;
  }}).join("");
}}

function refreshSmartSubcategories(preserve = false) {{
  const shelf = byId("shelf").value;
  const selected = preserve ? byId("smart-subcategory").value : "";
  const counts = new Map();
  if (shelf) {{
    for (const [name, count] of SUB_CATEGORY_COUNTS[shelf] || []) {{
      counts.set(name, count);
    }}
  }} else {{
    for (const items of Object.values(SUB_CATEGORY_COUNTS)) {{
      for (const [name, count] of items) {{
        counts.set(name, (counts.get(name) || 0) + count);
      }}
    }}
  }}
  const entries = [...counts.entries()].sort(
    (left, right) => right[1] - left[1] || left[0].localeCompare(right[0], "zh-CN")
  );
  const select = byId("smart-subcategory");
  select.innerHTML = '<option value="">全部二级分类</option>' + entries
    .map(([name, count]) => `<option value="${{esc(name)}}">${{esc(name)}} (${{count.toLocaleString()}})</option>`)
    .join("");
  select.value = entries.some(([name]) => name === selected) ? selected : "";
  state.smartSubcategory = select.value;
}}

function applyFilters() {{
  const query = byId("search").value.trim().toLocaleLowerCase();
  const language = byId("language").value;
  const shelf = byId("shelf").value;
  const smartSubcategory = byId("smart-subcategory").value;
  const author = byId("author").value.trim().toLocaleLowerCase();
  const tag = byId("tag").value;
  const rating = byId("rating").value;
  const duplicate = byId("duplicate").value;
  const collectionId = state.collectionId;
  const collection = COLLECTIONS.find((item) => String(item.id) === collectionId);
  const collectionPaths = new Set(
    (collection?.paths || []).map(pathKey)
  );
  state.filtered = DATA.filter((item) => {{
    if (state.duplicateGroup && item.g !== state.duplicateGroup) return false;
    if (state.formatTab === "OTHER") {{
      if (["EPUB", "PDF", "MOBI", "AZW3", "TXT"].includes(item.f)) return false;
    }} else if (state.formatTab && item.f !== state.formatTab) {{
      return false;
    }}
    if (query && !searchable(item).includes(query)) return false;
    if (language && item.fl !== language) return false;
    if (shelf && item.s !== shelf) return false;
    if (smartSubcategory && item.ss !== smartSubcategory) return false;
    if (tag && !(item.tags || []).includes(tag)) return false;
    if (author === "未知" && item.a) return false;
    if (author && author !== "未知" && !item.a.toLocaleLowerCase().includes(author)) return false;
    if (rating === "rated" && item.r === null) return false;
    if (rating === "unrated" && item.r !== null) return false;
    if (rating === "9" && (item.r === null || item.r < 9)) return false;
    if (rating === "8" && (item.r === null || item.r < 8)) return false;
    if (duplicate === "yes" && !item.d) return false;
    if (duplicate === "no" && item.d) return false;
    if (collection && !collectionPaths.has(pathKey(item.p))) return false;
    return true;
  }});
  state.sort = byId("size-sort").value;
  if (state.sort) {{
    const direction = state.sort === "size-asc" ? 1 : -1;
    state.filtered.sort((left, right) => {{
      const leftSize = Number(left.sz ?? left.z * 1024 * 1024);
      const rightSize = Number(right.sz ?? right.z * 1024 * 1024);
      return (leftSize - rightSize) * direction || left.t.localeCompare(right.t, "zh-CN");
    }});
  }}
  state.page = 1;
  render();
}}

function render() {{
  const totalPages = Math.max(1, Math.ceil(state.filtered.length / state.pageSize));
  state.page = Math.min(state.page, totalPages);
  const start = (state.page - 1) * state.pageSize;
  const pageRows = state.filtered.slice(start, start + state.pageSize);
  visiblePagePaths = pageRows.map((item) => item.p);
  const cardMode = state.viewMode === "cards";
  byId("table-wrap").hidden = cardMode;
  byId("card-grid").hidden = !cardMode;
  byId("list-view").classList.toggle("active", !cardMode);
  byId("card-view").classList.toggle("active", cardMode);
  if (cardMode) {{
    renderCards(pageRows);
    byId("rows").innerHTML = "";
  }} else {{
    byId("rows").innerHTML = pageRows.map((item) => `
    <tr class="${{selectedPaths.has(item.p) ? "bulk-selected" : ""}}">
      <td>
        <input class="select-column select-book" type="checkbox" data-path="${{esc(item.p)}}"
               ${{selectedPaths.has(item.p) ? "checked" : ""}} aria-label="选择《${{esc(item.t)}}》">
      </td>
      <td>
        <div class="book-cell">
          ${{
            item.cv
              ? `<img class="cover" src="${{esc(item.cv)}}" alt="${{esc(item.t)}}" loading="lazy"
                   onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
                 <div class="cover-placeholder">${{esc(item.f)}}</div>`
              : `<div class="cover-placeholder" style="display:flex">${{esc(item.f)}}</div>`
          }}
          <div class="book-title">${{esc(item.t)}}</div>
        </div>
      </td>
      <td>${{esc(item.a || "未知")}}</td>
      <td><span class="format">${{esc(item.f)}}</span></td>
      <td class="file-size">${{formatFileSize(item.sz)}}</td>
      <td>${{esc(item.fl)}}</td>
      <td>
        ${{
          item.r !== null
            ? `<a class="rating" href="${{esc(item.u)}}" target="_blank" rel="noopener"
                  title="${{esc(item.mt)}}">${{Number(item.r).toFixed(1)}}</a>
               <span class="rating-count">${{formatVotes(item.rc)}}</span>`
            : '<span class="muted">未获取</span>'
        }}
      </td>
      <td>${{esc(item.s)}}</td>
      <td>
        ${{esc(item.ss || item.c || "未分类")}}
        ${{duplicateTag(item)}}
        ${{(item.tags || []).map((tag) => `<span class="tag content-tag">${{esc(tag)}}</span>`).join("")}}
        ${{item.q ? `<span class="tag">${{esc(item.q)}}</span>` : ""}}
        ${{
          item.f !== "PDF" && aiSummaryFor(item)
            ? `<div class="list-ai"><strong>AI简介：</strong>${{esc(aiSummaryFor(item).summary)}}</div>
               <div class="list-ai-reason"><strong>推荐：</strong>${{esc(aiSummaryFor(item).reason)}}</div>`
            : ""
        }}
      </td>
      <td class="path">
        <span>${{esc(item.p)}}</span>
        <div class="path-actions">
          ${{
            (SERVER_MODE || BRIDGE_MODE)
              ? `<button class="open-book" type="button" data-path="${{esc(item.p)}}">打开</button>`
              : ""
          }}
          <button class="reveal-book" type="button" data-path="${{esc(item.p)}}">定位</button>
          <button class="douban-search" type="button" data-title="${{esc(item.t)}}">豆瓣</button>
          ${{
            BRIDGE_MODE && item.f !== "PDF" && !aiSummaryFor(item)
              ? `<button class="ai-generate" type="button" data-path="${{esc(item.p)}}">生成AI简介</button>`
              : ""
          }}
          <button class="copy-path" type="button" data-path="${{esc(item.ap)}}">复制路径</button>
          <button class="delete-open" type="button" data-path="${{esc(item.ap)}}">删除…</button>
        </div>
      </td>
    </tr>
    `).join("");
  }}
  byId("empty").hidden = state.filtered.length !== 0;
  byId("count").textContent = `${{state.filtered.length.toLocaleString()}} 条记录`;
  byId("page").textContent = `${{state.page}} / ${{totalPages}}`;
  byId("prev").disabled = state.page <= 1;
  byId("next").disabled = state.page >= totalPages;
  updateSelectionUi();
  byId("active-group").hidden = !state.duplicateGroup;
  if (state.duplicateGroup) {{
    const count = groupCounts.get(state.duplicateGroup) || 0;
    byId("active-group-text").textContent =
      `SHA-256 重复组 ${{state.duplicateGroup}} · ${{count}} 个文件`;
  }}
}}

function updateFormatTabs() {{
  const primary = new Set(["EPUB", "PDF", "MOBI", "AZW3", "TXT"]);
  const counts = new Map();
  for (const item of DATA) {{
    counts.set(item.f, (counts.get(item.f) || 0) + 1);
  }}
  const otherCount = DATA.filter((item) => !primary.has(item.f)).length;
  for (const button of document.querySelectorAll(".format-tab")) {{
    if (button.id === "author-stats-open") continue;
    const value = button.dataset.format || "";
    const count = value === ""
      ? DATA.length
      : value === "OTHER"
        ? otherCount
        : (counts.get(value) || 0);
    button.classList.toggle("active", state.formatTab === value);
    const badge = button.querySelector("span");
    if (badge) badge.textContent = count.toLocaleString();
  }}
}}

function authorStats() {{
  const counts = new Map();
  for (const item of DATA) {{
    if (!item.a) continue;
    counts.set(item.a, (counts.get(item.a) || 0) + 1);
  }}
  return [...counts.entries()]
    .map(([author, count]) => ({{ author, count }}))
    .sort((left, right) => right.count - left.count || left.author.localeCompare(right.author, "zh-CN"));
}}

function renderAuthorStats(query = "") {{
  const term = query.trim().toLocaleLowerCase();
  const stats = authorStats().filter((item) => !term || item.author.toLocaleLowerCase().includes(term));
  const unknownCount = DATA.filter((item) => !item.a).length;
  byId("author-stats-summary").textContent =
    `${{stats.length.toLocaleString()}} 位作者 · 按书籍数量从多到少` +
    (unknownCount ? ` · 另有 ${{unknownCount.toLocaleString()}} 本作者未知` : "");
  byId("author-stats-list").innerHTML = stats.map((item) => `
    <div class="author-stat-row">
      <span class="author-stat-name">${{esc(item.author)}}</span>
      <span class="author-stat-count">${{item.count.toLocaleString()}} 本</span>
      <button type="button" data-author="${{esc(item.author === "作者未知" ? "未知" : item.author)}}">筛选</button>
    </div>
  `).join("");
}}

function openAuthorStats() {{
  renderAuthorStats("");
  byId("author-stats-search").value = "";
  byId("author-stats-modal").hidden = false;
  byId("author-stats-search").focus();
}}

function closeAuthorStats() {{
  byId("author-stats-modal").hidden = true;
}}

function clearFilterControls() {{
  byId("search").value = "";
  byId("language").value = "";
  byId("shelf").value = "";
  byId("smart-subcategory").value = "";
  byId("author").value = "";
  byId("tag").value = "";
  byId("rating").value = "";
  byId("duplicate").value = "";
  byId("size-sort").value = "";
  state.duplicateGroup = "";
  state.collectionId = "";
  state.sort = "";
  state.formatTab = "EPUB";
  state.smartSubcategory = "";
  refreshSmartSubcategories(false);
  refreshCollectionControls();
  updateFormatTabs();
}}

function showDuplicateGroup(group) {{
  clearFilterControls();
  state.duplicateGroup = group;
  applyFilters();
  showToast("已筛选出 SHA-256 完全相同的文件");
}}

let pendingDeletePath = "";
let pendingDeletePaths = [];
let libraryRootHandle = null;
let libraryHandleLoaded = false;
let serverVersion = null;

function openHandleDb() {{
  return new Promise((resolve, reject) => {{
    const request = indexedDB.open(HANDLE_DB, 1);
    request.onupgradeneeded = () => {{
      if (!request.result.objectStoreNames.contains(HANDLE_STORE)) {{
        request.result.createObjectStore(HANDLE_STORE);
      }}
    }};
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  }});
}}

async function saveLibraryHandle(handle) {{
  const db = await openHandleDb();
  await new Promise((resolve, reject) => {{
    const transaction = db.transaction(HANDLE_STORE, "readwrite");
    transaction.objectStore(HANDLE_STORE).put(handle, "root");
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  }});
}}

async function loadLibraryHandle() {{
  const db = await openHandleDb();
  return new Promise((resolve, reject) => {{
    const transaction = db.transaction(HANDLE_STORE, "readonly");
    const request = transaction.objectStore(HANDLE_STORE).get("root");
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  }});
}}

function setLibraryAccess(connected, message) {{
  if (SERVER_MODE) {{
    byId("access-dot").classList.add("connected");
    byId("access-status").textContent = "动态服务已连接 · 使用“刷新书库”手动刷新";
    byId("connect-library").hidden = true;
    return;
  }}
  byId("access-dot").classList.toggle("connected", connected);
  byId("access-status").textContent = message;
  byId("connect-library").textContent = connected ? "重新连接书库文件夹" : "连接书库文件夹";
}}

async function pollServerStatus(showFailure = false) {{
  try {{
    const response = await fetch("/api/status", {{ cache: "no-store" }});
    if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
    const status = await response.json();
    serverVersion = status.version;
    byId("sync-status").textContent =
      `动态服务 · ${{status.book_count.toLocaleString()}} 本 · 扫描 ${{status.last_scan || "…"}}` +
      (status.scanning ? " · 正在更新" : "");
    return status;
  }} catch (error) {{
    byId("sync-status").textContent = "动态服务暂时不可用";
    if (showFailure) showToast(`服务状态读取失败：${{error.message}}`);
    return null;
  }}
}}

function startServerPolling() {{
  setLibraryAccess(true, "");
  pollServerStatus();
}}

async function detectBookBackend() {{
  if (!/^https?:$/.test(location.protocol)) return false;
  try {{
    const response = await fetch("/api/status", {{ cache: "no-store" }});
    if (!response.ok) return false;
    const status = await response.json();
    SERVER_MODE =
      status.ok &&
      status.service === "book-library" &&
      status.root === LIBRARY_ROOT;
    return SERVER_MODE;
  }} catch {{
    return false;
  }}
}}

async function detectBridge() {{
  try {{
    const response = await fetch(`${{BRIDGE_URL}}/api/status`, {{ cache: "no-store" }});
    if (!response.ok) return false;
    const status = await response.json();
    BRIDGE_MODE = status.ok && status.service === "book-bridge";
    if (BRIDGE_MODE) {{
      AI_BATCH_PROGRESS = status.batch || null;
      await loadAISummaries();
      await loadCollections();
      updateBridgeStatus();
    }}
    return BRIDGE_MODE;
  }} catch {{
    BRIDGE_MODE = false;
    return false;
  }}
}}

async function loadAISummaries() {{
  try {{
    const response = await fetch(`${{BRIDGE_URL}}/api/ai-summaries`, {{ cache: "no-store" }});
    if (!response.ok) return;
    const payload = await response.json();
    AI_SUMMARIES = new Map(Object.entries(payload.items || {{}}));
  }} catch {{
    AI_SUMMARIES = new Map();
  }}
}}

function updateBridgeStatus() {{
  const prefix = SERVER_MODE ? "动态服务" : "静态页面";
  let text =
    `${{prefix}} · AI桥接已连接 · 已生成简介 ${{AI_SUMMARIES.size.toLocaleString()}} 本`;
  if (AI_BATCH_PROGRESS) {{
    const done = Number(AI_BATCH_PROGRESS.completed || 0) + Number(AI_BATCH_PROGRESS.errors || 0);
    const total = Number(AI_BATCH_PROGRESS.total || 0);
    text += ` · 批量 ${{done.toLocaleString()}}/${{total.toLocaleString()}}`;
  }}
  byId("sync-status").textContent = text;
}}

function aiKey(item) {{
  return String(item.p || "").replace(/\//g, "\\").toLocaleLowerCase();
}}

async function requestAISummary(item) {{
  if (!BRIDGE_MODE) throw new Error("AI桥接服务未运行");
  if (item.f === "PDF") throw new Error("PDF 书籍不生成 AI 简介");
  const response = await fetch(`${{BRIDGE_URL}}/api/ai-summary`, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      path: item.p,
      title: item.t,
      author: item.a,
      category: item.s,
      tags: item.tags || []
    }})
  }});
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "AI生成失败");
  AI_SUMMARIES.set(aiKey(item), {{
    summary: payload.summary,
    reason: payload.reason,
    model: payload.model
  }});
  return payload;
}}

async function verifyLibraryRoot(handle) {{
  const curation = await handle.getDirectoryHandle(".book-library");
  await curation.getFileHandle("书库总览.html");
}}

async function connectLibrary() {{
  if (typeof window.showDirectoryPicker !== "function") {{
    setLibraryAccess(false, "当前浏览器不支持直接删除文件，请改用 Chrome 或 Edge");
    return null;
  }}
  try {{
    const handle = await window.showDirectoryPicker({{
      id: "book-library-root",
      mode: "readwrite"
    }});
    await verifyLibraryRoot(handle);
    libraryRootHandle = handle;
    await saveLibraryHandle(handle);
    setLibraryAccess(true, `已连接：${{handle.name}}`);
    showToast("书库文件夹已连接");
    return handle;
  }} catch (error) {{
    if (error.name === "AbortError") return null;
    setLibraryAccess(false, "请选择包含“.book-library”的书籍根目录");
  showToast("连接失败：请选择书库根目录");
    return null;
  }}
}}

async function ensureLibraryAccess() {{
  if (!libraryRootHandle) return connectLibrary();
  let permission = await libraryRootHandle.queryPermission({{ mode: "readwrite" }});
  if (permission === "granted") return libraryRootHandle;
  permission = await libraryRootHandle.requestPermission({{ mode: "readwrite" }});
  if (permission === "granted") {{
    setLibraryAccess(true, `已连接：${{libraryRootHandle.name}}`);
    return libraryRootHandle;
  }}
  return null;
}}

async function recycleLibraryFiles(relativePaths) {{
  const endpoint = collectionApiPath("/api/recycle");
  if (!endpoint) throw new Error("请先启动本地服务");
  const response = await fetch(endpoint, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ paths: relativePaths }})
  }});
  const payload = await response.json();
  if (!response.ok || !payload.ok) {{
    throw new Error(payload.error || "移入回收站失败");
  }}
  if (payload.version) serverVersion = payload.version;
  return payload;
}}

function removeBooksFromView(absolutePaths) {{
  const removed = new Set(absolutePaths);
  DATA = DATA.filter((item) => !removed.has(item.ap));
  selectedPaths = new Set(
    [...selectedPaths].filter((path) => DATA.some((item) => item.p === path))
  );
  rebuildGroupCounts();
  if (state.duplicateGroup && !groupCounts.has(state.duplicateGroup)) {{
    state.duplicateGroup = "";
  }}
  applyFilters();
  updateSummary();
}}

function removeBookFromView(path) {{
  removeBooksFromView([path]);
}}

function showDeleteModal(items) {{
  if (!items.length) return;
  pendingDeletePaths = items.map((item) => item.p);
  pendingDeletePath = items.length === 1 ? items[0].ap : "";
  byId("delete-modal-title").textContent = items.length === 1
    ? "移入回收站确认"
    : `移入回收站确认（${{items.length.toLocaleString()}} 本）`;
  byId("delete-modal-book").textContent = items.length === 1
    ? items[0].t
    : `已选择 ${{items.length.toLocaleString()}} 本书籍`;
  byId("delete-modal-path").textContent = items.slice(0, 8).map((item) => item.ap).join("\n") +
    (items.length > 8 ? `\n…另有 ${{items.length - 8}} 本` : "");
  byId("delete-ack").checked = false;
  const supported = Boolean(collectionApiPath("/api/recycle"));
  byId("confirm-delete").disabled = true;
  byId("confirm-delete").textContent = supported ? "移入回收站" : "本地服务未启动";
  byId("delete-warning").textContent =
    "所选文件会移入 Windows 回收站，并从书库记录与书集成员中移除。";
  byId("delete-ack-text").textContent = items.length === 1
    ? "我确认将该书籍移入 Windows 回收站"
    : `我确认将所选 ${{items.length.toLocaleString()}} 本书籍移入 Windows 回收站`;
  byId("hide-record").hidden = items.length !== 1;
  byId("delete-modal").hidden = false;
  byId("delete-ack").focus();
}}

function openDeleteModal(path) {{
  const item = DATA.find((book) => book.ap === path);
  if (item) showDeleteModal([item]);
}}

function openSelectedDeleteModal() {{
  const items = DATA.filter((item) => selectedPaths.has(item.p));
  if (!items.length) {{
    showToast("请先选择书籍");
    return;
  }}
  showDeleteModal(items);
}}

function closeDeleteModal() {{
  pendingDeletePath = "";
  pendingDeletePaths = [];
  byId("delete-modal").hidden = true;
  byId("delete-ack").checked = false;
  byId("confirm-delete").disabled = true;
}}

async function initializeLibraryAccess() {{
  await detectBookBackend();
  if (SERVER_MODE) {{
    startServerPolling();
    const hasBridge = await detectBridge();
    if (!hasBridge) await loadCollections();
    applyFilters();
    return;
  }}
  const hasBridge = await detectBridge();
  if (!hasBridge) byId("sync-status").textContent = "静态服务 · 连接文件夹后可手动刷新";
  if (typeof window.showDirectoryPicker !== "function") {{
    setLibraryAccess(false, "当前浏览器不支持直接删除文件，请改用 Chrome 或 Edge");
    libraryHandleLoaded = true;
    applyFilters();
    return;
  }}
  try {{
    const handle = await loadLibraryHandle();
    if (!handle) {{
      setLibraryAccess(false, "删除书籍前需要授权书库根目录");
    }} else {{
      libraryRootHandle = handle;
      const permission = await handle.queryPermission({{ mode: "readwrite" }});
      setLibraryAccess(
        permission === "granted",
        permission === "granted"
          ? `已连接：${{handle.name}}`
          : "已记住书库文件夹，删除时会再次请求权限"
      );
    }}
  }} catch {{
    setLibraryAccess(false, "删除书籍前需要授权书库根目录");
  }} finally {{
    libraryHandleLoaded = true;
    applyFilters();
  }}
}}

byId("search").addEventListener("input", applyFilters);
byId("language").addEventListener("change", applyFilters);
byId("shelf").addEventListener("change", () => {{
  refreshSmartSubcategories(false);
  applyFilters();
}});
byId("smart-subcategory").addEventListener("change", applyFilters);
byId("author").addEventListener("input", applyFilters);
byId("tag").addEventListener("change", applyFilters);
byId("rating").addEventListener("change", applyFilters);
byId("duplicate").addEventListener("change", applyFilters);
byId("size-sort").addEventListener("change", applyFilters);
byId("collection-tabs").addEventListener("click", (event) => {{
  const button = event.target.closest(".collection-tab");
  if (!button) return;
  state.collectionId = button.dataset.collection || "";
  refreshCollectionControls();
  applyFilters();
}});
byId("create-collection").addEventListener("click", createCollection);
byId("collection-name").addEventListener("keydown", (event) => {{
  if (event.key === "Enter") createCollection();
}});
byId("bulk-add").addEventListener("click", addSelectedToCollection);
byId("bulk-delete").addEventListener("click", openSelectedDeleteModal);
byId("bulk-clear").addEventListener("click", () => {{
  selectedPaths.clear();
  render();
}});
byId("select-page").addEventListener("click", () => {{
  const pageSelected = visiblePagePaths.length > 0 &&
    visiblePagePaths.every((path) => selectedPaths.has(path));
  selectCurrentPage(!pageSelected);
}});
byId("select-visible").addEventListener("change", (event) => {{
  selectCurrentPage(event.target.checked);
}});
document.addEventListener("change", (event) => {{
  const checkbox = event.target.closest(".select-book");
  if (!checkbox) return;
  if (checkbox.checked) selectedPaths.add(checkbox.dataset.path);
  else selectedPaths.delete(checkbox.dataset.path);
  checkbox.closest("tr, article")?.classList.toggle("bulk-selected", checkbox.checked);
  updateSelectionUi();
}});
byId("theme-toggle").addEventListener("click", toggleTheme);
document.querySelector(".format-tabs").addEventListener("click", (event) => {{
  const button = event.target.closest(".format-tab");
  if (!button) return;
  state.formatTab = button.dataset.format || "";
  state.page = 1;
  updateFormatTabs();
  applyFilters();
}});
byId("list-view").addEventListener("click", () => {{
  state.viewMode = "list";
  try {{ localStorage.setItem("book-library-view", "list"); }} catch {{}}
  render();
}});
byId("card-view").addEventListener("click", () => {{
  state.viewMode = "cards";
  try {{ localStorage.setItem("book-library-view", "cards"); }} catch {{}}
  render();
}});
byId("card-grid").addEventListener("click", async (event) => {{
  const cover = event.target.closest(".card-cover, .card-cover-placeholder");
  if (cover) {{
    window.clearTimeout(coverClickTimer);
    if (event.detail > 1) return;
    coverClickTimer = window.setTimeout(() => {{
      openDoubanDrawer(cover.dataset.doubanTitle || "");
    }}, 350);
    return;
  }}
  const aiButton = event.target.closest(".ai-generate");
  if (aiButton) {{
    const item = DATA.find((book) => book.p === aiButton.dataset.path);
    if (!item) return;
    aiButton.disabled = true;
    aiButton.textContent = "生成中…";
    try {{
      await requestAISummary(item);
      render();
      showToast("AI 简介已生成");
    }} catch (error) {{
      aiButton.disabled = false;
      aiButton.textContent = "生成AI简介";
      showToast(error.message);
    }}
    return;
  }}
  const openButton = event.target.closest(".open-book");
  if (openButton) {{
    try {{
      await openBook(openButton.dataset.path);
      showToast("已调用默认软件打开");
    }} catch (error) {{
      showToast(error.message);
    }}
    return;
  }}
  const revealButton = event.target.closest(".reveal-book");
  if (revealButton) {{
    try {{
      await revealBook(revealButton.dataset.path);
      showToast("已在文件管理器中定位");
    }} catch (error) {{
      showToast(error.message);
    }}
    return;
  }}
  const doubanButton = event.target.closest(".douban-search");
  if (doubanButton) {{
    openDoubanDrawer(doubanButton.dataset.title);
    return;
  }}
  const copyButton = event.target.closest(".copy-path");
  if (copyButton) {{
    try {{
      await copyPath(copyButton.dataset.path);
      showToast("路径已复制");
    }} catch {{
      showToast("复制失败，请手动选择路径");
    }}
    return;
  }}
  const deleteButton = event.target.closest(".delete-open");
  if (deleteButton) openDeleteModal(deleteButton.dataset.path);
}});
byId("card-grid").addEventListener("dblclick", async (event) => {{
  const target = event.target.closest("[data-open-path]");
  if (!target) return;
  window.clearTimeout(coverClickTimer);
  try {{
    await openBook(target.dataset.openPath);
    showToast("已调用默认软件打开");
  }} catch (error) {{
    showToast(error.message);
  }}
}});
byId("author-stats-open").addEventListener("click", openAuthorStats);
byId("author-stats-close").addEventListener("click", closeAuthorStats);
byId("author-stats-search").addEventListener("input", (event) => {{
  renderAuthorStats(event.target.value);
}});
byId("author-stats-list").addEventListener("click", (event) => {{
  const button = event.target.closest("[data-author]");
  if (!button) return;
  byId("author").value = button.dataset.author;
  closeAuthorStats();
  applyFilters();
}});
byId("author-stats-modal").addEventListener("click", (event) => {{
  if (event.target === byId("author-stats-modal")) closeAuthorStats();
}});
byId("douban-drawer-close").addEventListener("click", closeDoubanDrawer);
byId("reset").addEventListener("click", () => {{
  clearFilterControls();
  applyFilters();
}});
byId("clear-group").addEventListener("click", () => {{
  state.duplicateGroup = "";
  applyFilters();
}});
byId("rows").addEventListener("click", async (event) => {{
  const duplicateButton = event.target.closest(".dup-link");
  if (duplicateButton) {{
    showDuplicateGroup(duplicateButton.dataset.group);
    return;
  }}
  const deleteButton = event.target.closest(".delete-open");
  if (deleteButton) {{
    openDeleteModal(deleteButton.dataset.path);
    return;
  }}
  const aiButton = event.target.closest(".ai-generate");
  if (aiButton) {{
    const item = DATA.find((book) => book.p === aiButton.dataset.path);
    if (!item) return;
    aiButton.disabled = true;
    aiButton.textContent = "生成中…";
    try {{
      await requestAISummary(item);
      render();
      showToast("AI 简介已生成");
    }} catch (error) {{
      aiButton.disabled = false;
      aiButton.textContent = "生成AI简介";
      showToast(error.message);
    }}
    return;
  }}
  const openButton = event.target.closest(".open-book");
  if (openButton) {{
    try {{
      await openBook(openButton.dataset.path);
      showToast("已调用默认软件打开");
    }} catch (error) {{
      showToast(error.message);
    }}
    return;
  }}
  const revealButton = event.target.closest(".reveal-book");
  if (revealButton) {{
    try {{
      await revealBook(revealButton.dataset.path);
      showToast("已在文件管理器中定位");
    }} catch (error) {{
      showToast(error.message);
    }}
    return;
  }}
  const doubanButton = event.target.closest(".douban-search");
  if (doubanButton) {{
    openDoubanDrawer(doubanButton.dataset.title);
    return;
  }}
  const copyButton = event.target.closest(".copy-path");
  if (!copyButton) return;
  try {{
    await copyPath(copyButton.dataset.path);
    showToast("路径已复制");
  }} catch {{
    showToast("复制失败，请手动选择路径");
  }}
}});
byId("connect-library").addEventListener("click", connectLibrary);
byId("sync-library").addEventListener("click", async () => {{
  await syncLibrary({{ interactive: true, silent: false }});
}});
byId("refresh-ai").addEventListener("click", async () => {{
  await detectBridge();
  applyFilters();
  showToast(`已刷新 AI 简介：${{AI_SUMMARIES.size.toLocaleString()}} 本`);
}});
byId("restore-hidden").addEventListener("click", () => {{
  hiddenRecordPaths.clear();
  savePathSet(HIDDEN_RECORD_KEY, hiddenRecordPaths);
  DATA = RAW_DATA.filter((item) => !deletedFilePaths.has(item.ap));
  rebuildGroupCounts();
  applyFilters();
  updateSummary();
  showToast("已恢复隐藏的目录记录");
}});
byId("cancel-delete").addEventListener("click", closeDeleteModal);
byId("delete-modal").addEventListener("click", (event) => {{
  if (event.target === byId("delete-modal")) closeDeleteModal();
}});
document.addEventListener("keydown", (event) => {{
  if (event.key === "Escape" && !byId("delete-modal").hidden) closeDeleteModal();
  if (event.key === "Escape" && !byId("author-stats-modal").hidden) closeAuthorStats();
  if (event.key === "Escape" && !byId("douban-drawer").hidden) closeDoubanDrawer();
}});
byId("delete-ack").addEventListener("change", () => {{
  const supported = Boolean(collectionApiPath("/api/recycle"));
  byId("confirm-delete").disabled = !byId("delete-ack").checked || !supported;
}});
byId("hide-record").addEventListener("click", () => {{
  if (!pendingDeletePath) return;
  hiddenRecordPaths.add(pendingDeletePath);
  savePathSet(HIDDEN_RECORD_KEY, hiddenRecordPaths);
  removeBookFromView(pendingDeletePath);
  closeDeleteModal();
  showToast("记录已隐藏，原始文件仍然保留");
}});
byId("confirm-delete").addEventListener("click", async () => {{
  if (!pendingDeletePaths.length || !byId("delete-ack").checked) return;
  const items = DATA.filter((item) => pendingDeletePaths.includes(item.p));
  if (!items.length) return;
  const button = byId("confirm-delete");
  button.disabled = true;
  button.textContent = "正在移入回收站…";
  try {{
    await recycleLibraryFiles(items.map((item) => item.p));
    for (const item of items) {{
      deletedFilePaths.add(item.ap);
      hiddenRecordPaths.delete(item.ap);
    }}
    savePathSet(DELETED_FILE_KEY, deletedFilePaths);
    savePathSet(HIDDEN_RECORD_KEY, hiddenRecordPaths);
    removeBooksFromView(items.map((item) => item.ap));
    closeDeleteModal();
    await loadCollections();
    showToast(`已将 ${{items.length.toLocaleString()}} 本书移入 Windows 回收站`);
  }} catch (error) {{
    showToast(error.name === "AbortError" ? "已取消删除" : `删除失败：${{error.message}}`);
    button.textContent = "移入回收站";
    button.disabled = !byId("delete-ack").checked;
  }}
}});
byId("prev").addEventListener("click", () => {{ state.page -= 1; render(); }});
byId("next").addEventListener("click", () => {{ state.page += 1; render(); }});
try {{
  state.viewMode = localStorage.getItem("book-library-view") === "cards" ? "cards" : "list";
}} catch {{}}
updateThemeToggle();
rebuildGroupCounts();
updateSummary();
refreshSmartSubcategories(false);
initializeLibraryAccess();
applyFilters();
</script>
</body>
</html>
"""
    (output_dir / "书库总览.html").write_text(document, encoding="utf-8")


def main() -> int:
    configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=library_paths.ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=library_paths.DATA_DIR,
    )
    parser.add_argument("--hash-workers", type=int, default=4)
    parser.add_argument("--meta-workers", type=int, default=8)
    parser.add_argument("--skip-pdf-metadata", action="store_true")
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="Reuse the existing catalog and regenerate HTML/report only.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.html_only:
        catalog_path = output / "exports" / "图书总目录.csv"
        if not catalog_path.exists():
            catalog_path = output / "图书总目录.csv"
        if not catalog_path.exists():
            raise SystemExit("图书总目录.csv does not exist")
        books = load_books_from_catalog(catalog_path, root)
        exact_groups: dict[str, list[Book]] = defaultdict(list)
        title_groups_set: set[str] = set()
        for book in books:
            if book.exact_group:
                exact_groups[book.exact_group].append(book)
            if book.title_group:
                title_groups_set.add(book.title_group)
        duplicate_bytes = sum(
            group[0].size * (len(group) - 1) for group in exact_groups.values()
        )
        generate_markdown_report(
            output,
            books,
            len(exact_groups),
            duplicate_bytes,
            len(title_groups_set),
        )
        generate_html(output, books)
        print(
            f"已根据现有目录重新生成 HTML 与报告：{len(books):,} 个文件",
            flush=True,
        )
        return 0

    pdfinfo_path = ""
    if not args.skip_pdf_metadata:
        candidate = (
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "native"
            / "poppler"
            / "Library"
            / "bin"
            / "pdfinfo.exe"
        )
        if candidate.exists():
            pdfinfo_path = str(candidate)

    print("扫描书籍文件...", flush=True)
    paths = iter_book_paths(root)
    books = [build_base_book(path, root) for path in paths]
    print(f"发现 {len(books):,} 个书刊/资料文件", flush=True)

    print("读取内嵌元数据...", flush=True)
    enrich_metadata(books, root, pdfinfo_path, max(1, args.meta_workers))

    print("检测精确重复...", flush=True)
    duplicate_groups, duplicate_bytes = mark_exact_duplicates(
        books, max(1, args.hash_workers)
    )

    for book in books:
        add_quality_flags(book)
    title_groups = mark_title_duplicates(books)
    attach_offline_assets(books, output)

    print("生成目录与报告...", flush=True)
    generate_csv_reports(
        output, books, duplicate_groups, duplicate_bytes, title_groups
    )
    library_store.sync_catalog(
        output / "exports" / "图书总目录.csv",
        output / "library.db",
        root,
    )
    library_store.export_catalog_csv(
        output / "library.db",
        output / "exports" / "图书总目录.csv",
    )
    generate_markdown_report(
        output, books, duplicate_groups, duplicate_bytes, title_groups
    )
    generate_html(output, books)
    print(
        f"完成：{len(books):,} 个文件，{duplicate_groups:,} 个精确重复组，"
        f"可回收 {format_bytes(duplicate_bytes)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
