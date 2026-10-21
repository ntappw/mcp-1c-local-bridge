"""Где лежат каталог обмена с обработкой 1С и файлы больших результатов."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

EXCHANGE_DIR_NAME = "mcp-1c-local-bridge"


def exchange_root() -> Path:
    """Каталог обмена: %TEMP%\\mcp-1c-local-bridge пользователя Windows (там же его ищет обработка)."""
    override = os.environ.get("MCP_1C_BRIDGE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("TEMP") or tempfile.gettempdir()) / EXCHANGE_DIR_NAME
    return _windows_temp_from_wsl() / EXCHANGE_DIR_NAME


def results_dir() -> Path:
    """Каталог, куда пишутся результаты, не поместившиеся в ответ инструмента."""
    override = os.environ.get("MCP_1C_BRIDGE_RESULTS")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / EXCHANGE_DIR_NAME / "results"


@lru_cache(maxsize=1)
def _windows_temp_from_wsl() -> Path:
    # %TEMP% бывает с короткими именами 8.3 (IVANOV~1.PET); drvfs их понимает, но полный путь нагляднее
    windows_path = _interop([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; (Get-Item -LiteralPath $env:TEMP).FullName",
    ])
    if not windows_path:
        windows_path = _interop(["cmd.exe", "/c", "echo %TEMP%"])
    if not windows_path:
        raise RuntimeError("Не удалось узнать каталог TEMP Windows. "
                           "Задайте каталог обмена переменной MCP_1C_BRIDGE_DIR.")
    linux_path = _interop(["wslpath", "-u", windows_path])
    if not linux_path:
        raise RuntimeError(f"wslpath не преобразовал путь {windows_path}. Задайте переменную MCP_1C_BRIDGE_DIR.")
    return Path(linux_path)


def _interop(args: list[str]) -> str:
    if shutil.which(args[0]) is None:
        return ""
    # Из каталога файловой системы WSL cmd.exe ругается на UNC-путь, поэтому запускаем с диска C:
    cwd = "/mnt/c" if os.path.isdir("/mnt/c") else None
    try:
        completed = subprocess.run(args, capture_output=True, timeout=20, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in completed.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1] if completed.returncode == 0 and lines else ""
