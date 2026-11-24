"""Проверка обновлений: новая версия в репозитории, из которого установлен MCP-сервер, и устаревшая обработка."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .paths import results_dir

# Где пользователю взять новую версию: ссылку агент показывает как есть, по ней можно перейти
REPOSITORY_URL = "https://github.com/ntappw/mcp-1c-local-bridge"
CHECK_INTERVAL_SEC = 24 * 3600
# Первый раз (кэша ещё нет) ответ ждём недолго: дальше проверка идёт в фоне и не задерживает инструменты
FIRST_WAIT_SEC = 5.0
GIT_TIMEOUT_SEC = 20
_TAG = re.compile(r"refs/tags/v?(\d+)\.(\d+)\.(\d+)$")
_lock = threading.Lock()
_running = False

Runner = Callable[[list[str], Path], str]


def version_tuple(text: str) -> tuple[int, ...] | None:
    numbers = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(number) for number in numbers) if len(numbers) == 3 else None


def bridge_version_note(bridge_version: str, server_version: str = __version__) -> str:
    """Предупреждение, если обработка в сеансе и MCP-сервер разных версий; пусто, если версии совпадают."""
    bridge, server = version_tuple(bridge_version), version_tuple(server_version)
    if bridge is None or server is None or bridge == server:
        return ""
    if bridge < server:
        return (f"в этой базе открыта обработка моста версии {bridge_version}, а MCP-сервер уже версии "
                f"{server_version}: новые возможности здесь не работают. Закройте форму моста и откройте "
                f"MCPLocalBridge.epf версии {server_version} - файл лежит в папке dist репозитория {REPOSITORY_URL} "
                "(и в папке dist того клона, откуда запущен MCP-сервер)")
    return (f"в этой базе открыта обработка моста версии {bridge_version}, а MCP-сервер старее - версии "
            f"{server_version}. Обновите MCP-сервер: в папке клона репозитория {REPOSITORY_URL} выполните git pull "
            "и uv sync, затем перезапустите MCP-клиент")


def update_note(repo: Path | None = None, runner: Runner | None = None, now: float | None = None) -> str:
    """Строка о вышедшей новой версии MCP-сервера или пусто. Сеть не трогается чаще раза в сутки."""
    if os.environ.get("MCP_1C_BRIDGE_UPDATE_CHECK", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    repo = repo or _repo_root()
    if repo is None:
        return ""
    cache = _read_cache()
    current = time.time() if now is None else now
    fresh = cache.get("repo") == str(repo) and current - float(cache.get("checked") or 0) < CHECK_INTERVAL_SEC
    if not fresh:
        worker = threading.Thread(target=_refresh, args=(repo, runner or _git), daemon=True)
        with _lock:
            start = not _running
        if start:
            worker.start()
            if not cache:
                worker.join(FIRST_WAIT_SEC)
                cache = _read_cache()
    return _note_from(cache.get("latest") or "")


def _note_from(latest: str) -> str:
    newest, installed = version_tuple(latest), version_tuple(__version__)
    if newest is None or installed is None or newest <= installed:
        return ""
    return (f"Вышла новая версия моста {latest}, установлена {__version__}. Что нового - в репозитории "
            f"{REPOSITORY_URL}. Как обновиться: в папке клона репозитория выполнить git pull и uv sync, перезапустить "
            "MCP-клиент, затем в базе закрыть форму моста и открыть новый MCPLocalBridge.epf из папки dist.")


def _refresh(repo: Path, runner: Runner) -> None:
    global _running
    with _lock:
        if _running:
            return
        _running = True
    try:
        try:
            output = runner(["git", "ls-remote", "--tags", "--refs", "origin"], repo)
        except (OSError, subprocess.SubprocessError):
            output = None
        latest = _latest_tag(output) if output is not None else ""
        cache = {"repo": str(repo), "checked": time.time(), "latest": latest}
        if output is None:
            # Сети или доступа нет: повторим через сутки, прежний ответ не теряем
            cache["latest"] = _read_cache().get("latest") or ""
        _write_cache(cache)
    finally:
        with _lock:
            _running = False


def _latest_tag(output: str) -> str:
    versions = []
    for line in output.splitlines():
        match = _TAG.search(line.strip())
        if match:
            versions.append(tuple(int(part) for part in match.groups()))
    return ".".join(str(part) for part in max(versions)) if versions else ""


def _git(args: list[str], repo: Path) -> str:
    env = dict(os.environ)
    # Без вопросов о пароле и ключе: проверка идёт в фоне, отвечать на них некому
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    completed = subprocess.run(args, cwd=repo, env=env, capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=GIT_TIMEOUT_SEC)
    if completed.returncode != 0:
        raise subprocess.SubprocessError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout.decode("utf-8", "replace")


def _repo_root() -> Path | None:
    """Клон репозитория, из которого установлен сервер; установка не из клона - проверки нет."""
    for folder in Path(__file__).resolve().parents:
        if (folder / ".git").exists() and (folder / "pyproject.toml").exists():
            return folder
    return None


def _cache_file() -> Path:
    return results_dir().parent / "update-check.json"


def _read_cache() -> dict:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict) -> None:
    try:
        _cache_file().parent.mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
