# Book Library / 书库总览

[中文](#中文) | [English](#english)

## 中文

一个面向 Windows 的本地电子书库浏览、筛选和整理工具。它会扫描书库目录，生成离线索引页面，并提供 SQLite 元数据、书集、重复检测、豆瓣评分缓存和可选的 AI 简介。

页面默认由 [LocalDock](https://github.com/pmmxd/localdock) 提供本地静态托管。书籍文件、数据库、封面和缓存始终保留在本机。

### 功能

- 按格式、一级分类、二级分类、标签、语言、作者和豆瓣评分筛选。
- 列表与卡片视图，支持封面、文件大小显示和大小排序。
- 通过 SHA-256 检测字节级重复文件，并提供人工复核视图。
- 使用书集组织书籍；支持多选、批量加入书集和批量移入 Windows 回收站。
- SQLite 作为主索引，CSV 仅用于导出和兼容。
- 支持深色模式、路径复制、默认软件打开、文件管理器定位和豆瓣搜索抽屉。
- 可选 LocalDock 后端和本地 AI，为非 PDF 书籍生成简介与推荐理由。

### 隐私与数据

本仓库**不包含**用户的电子书、封面、数据库、AI 结果、日志、回收站、旧备份或本机绝对路径。

以下路径不会进入 Git：

- `data/`
- `legacy/`
- `*.db`、`*.db-wal`、`*.db-shm`、`*.sqlite`
- 封面、导出文件、缓存、运行日志和本机配置

生成的 `data/书库总览.html` 可能包含本机文件路径，因此也应只保存在本地。服务默认只绑定 `127.0.0.1`，不要将管理接口直接暴露到公网。

### 目录结构

```text
<BOOK_LIBRARY_ROOT>/
  books/                    # 你的书籍文件，不提交到 Git
  .book-library/            # 本项目
    app/
      bridge.py             # 本地系统操作与 AI 后端
      server.py             # 可选动态服务
      scripts/
      start_bridge.cmd
      start_library.cmd
    data/                   # 本地生成，已忽略
    legacy/                 # 旧数据备份，已忽略
```

### 环境要求

- Windows 10 或 Windows 11
- Python 3.11 或更高版本
- [LocalDock](https://github.com/pmmxd/localdock)
- 可选：Ollama 和本地模型，用于 AI 简介

如果使用书集、AI 或本地系统操作，安装 Python 依赖：

```powershell
python -m pip install requests
```

### 快速开始

1. 获取 LocalDock 和本项目：

```powershell
git clone https://github.com/pmmxd/localdock.git
git clone https://github.com/pmmxd/book-library.git "<BOOK_LIBRARY_ROOT>\.book-library"
```

2. 首次生成书库索引：

```powershell
Set-Location "<BOOK_LIBRARY_ROOT>\.book-library"
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data"
```

首次扫描会计算文件哈希并读取部分内嵌元数据，耗时取决于书库大小。生成的索引位于 `data/`。

3. 在 LocalDock 中添加站点：

| 配置项 | 值 |
| --- | --- |
| 名称 | Book Library |
| 根目录 | `<BOOK_LIBRARY_ROOT>\.book-library` |
| 入口文件 | `data/书库总览.html` |
| 文件监控 | 关闭 |
| 自动刷新页面 | 关闭 |

建议关闭监控和自动刷新，使用页面中的“刷新书库”按钮或手动重建索引，避免数据库写入触发页面反复重载。

4. 启动书库后端。书集、AI 简介和回收站删除等功能需要它：

```powershell
app\start_bridge.cmd
```

停止后端：

```powershell
app\stop_bridge.cmd
```

5. 在 LocalDock 中启动站点并打开入口页面。

### 手动更新

新增、移动或删除书籍后，可以在页面点击“刷新书库”。

需要重新计算封面、哈希、元数据和分类时，执行完整重建：

```powershell
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data"
```

仅根据现有目录重新生成 HTML：

```powershell
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data" --html-only
```

### 可选 AI

AI 功能通过本机 Ollama 调用，不会把书籍文件上传到云端。默认模型为 `qwen3.5:4b`；可在 `app/bridge.py` 的启动参数中修改模型。PDF 不生成 AI 简介。

### English

A local Windows application for browsing, filtering, and organizing an ebook collection. It scans a book library, builds a searchable HTML catalog, and maintains SQLite metadata, collections, duplicate detection, cached Douban ratings, and optional local AI summaries.

The page is intended to be hosted locally with [LocalDock](https://github.com/pmmxd/localdock). Books, databases, covers, caches, and logs stay on your machine.

#### Features

- Filter by format, primary and secondary category, tag, language, author, and rating.
- List and card views with covers, exact file sizes, and size sorting.
- SHA-256 duplicate detection with manual review views.
- Collections with multi-select, batch add, and Recycle Bin deletion.
- SQLite as the primary index; CSV is export-only.
- Dark mode, path copy, open with default app, reveal in File Explorer, and a Douban search drawer.
- Optional LocalDock integration and local AI summaries for non-PDF books.

#### Privacy And Data

This repository does not include user books, covers, databases, AI results, logs, recycle-bin contents, legacy backups, or machine-specific absolute paths.

The following are excluded from Git:

- `data/`
- `legacy/`
- `*.db`, `*.db-wal`, `*.db-shm`, and `*.sqlite`
- Generated covers, exports, caches, logs, and local configuration

The generated `data/书库总览.html` may contain local paths and should remain local. Services bind to `127.0.0.1` by default; do not expose management endpoints directly to the public internet.

#### Requirements

- Windows 10 or Windows 11
- Python 3.11+
- [LocalDock](https://github.com/pmmxd/localdock)
- Optional: Ollama and a local model for AI summaries

Install Python dependencies when using the local backend:

```powershell
python -m pip install requests
```

#### Setup

1. Clone LocalDock and this project:

```powershell
git clone https://github.com/pmmxd/localdock.git
git clone https://github.com/pmmxd/book-library.git "<BOOK_LIBRARY_ROOT>\.book-library"
```

2. Build the first local index:

```powershell
Set-Location "<BOOK_LIBRARY_ROOT>\.book-library"
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data"
```

3. Add a LocalDock site:

| Setting | Value |
| --- | --- |
| Name | Book Library |
| Root | `<BOOK_LIBRARY_ROOT>\.book-library` |
| Entry file | `data/书库总览.html` |
| File watch | Off |
| Auto reload | Off |

Disable watch and auto reload. Use the page refresh button or rebuild the index manually instead, so SQLite writes do not cause a reload loop.

4. Start the book backend for collections, AI, and Recycle Bin deletion:

```powershell
app\start_bridge.cmd
```

Stop it with:

```powershell
app\stop_bridge.cmd
```

5. Start the site in LocalDock and open the entry page.

#### Update The Index

Use the page refresh button after adding, moving, or removing books. To refresh covers, hashes, metadata, and classification, run the full build command again:

```powershell
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data"
```

Regenerate only the HTML from the existing catalog:

```powershell
python app\scripts\build_library_index.py --root "<BOOK_LIBRARY_ROOT>" --output "data" --html-only
```

#### Optional AI

AI summaries use a local Ollama endpoint and do not upload book files. The default model is `qwen3.5:4b` and can be changed in the `app/bridge.py` startup arguments. PDFs are excluded from AI summary generation.
