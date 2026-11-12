"""Canonical paths for the book library."""

from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
PROJECT_DIR = APP_DIR.parent
ROOT = PROJECT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "library.db"
HTML_PATH = DATA_DIR / "书库总览.html"
COVERS_DIR = DATA_DIR / "covers"
EXPORTS_DIR = DATA_DIR / "exports"
CACHE_DIR = DATA_DIR / "cache"
RUNTIME_DIR = DATA_DIR / "runtime"
LEGACY_DIR = PROJECT_DIR / "legacy"
