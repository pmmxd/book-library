#!/usr/bin/env python3
"""Move files to the Windows Recycle Bin without a permanent-delete fallback."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path


def send_to_recycle_bin(paths: list[Path]) -> None:
    sources = [path.resolve() for path in paths]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise OSError(f"文件不存在：{missing[0]}")
    payload = base64.b64encode(
        json.dumps([str(path) for path in sources], ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName Microsoft.VisualBasic
$json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))
$paths = $json | ConvertFrom-Json
foreach ($path in $paths) {{
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
        $path,
        'OnlyErrorDialogs',
        'SendToRecycleBin'
    )
}}
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "无法移入回收站").strip()
        raise OSError(detail)
