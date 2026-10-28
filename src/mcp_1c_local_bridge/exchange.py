"""Обмен командами с обработкой 1С через каталог (протокол версии 1, см. docs/protocol.md)."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROTOCOL = 1
# Обработка пишет session.json раз в 2 с; дольше 10 с тишины - форма не отвечает (закрыта, упала, модальное окно)
HEARTBEAT_ALIVE_SEC = 10.0
# Каталоги сеансов, молчащих дольше суток, остались от упавших клиентов - удаляем
STALE_PRUNE_SEC = 24 * 3600.0
CLOCK_PROBE = ".clock"
# Каталог сеанса обработка называет 8 шестнадцатеричными символами. Каталог обмена может быть общим
# (MCP_1C_BRIDGE_DIR), поэтому прочие папки в нём не считаются сеансами и не удаляются
SESSION_NAME = re.compile(r"[0-9a-f]{8}")


class BridgeError(Exception):
    """Ошибка, текст которой показывается агенту как есть."""


@dataclass
class Session:
    sid: str
    path: Path
    info: dict[str, Any]
    age: float  # секунд с последнего сердцебиения, по часам Windows

    @property
    def state(self) -> str:
        reported = self.info.get("state") or "ready"
        if reported == "busy":
            # Во время команды форма не может обновлять сердцебиение - тишина тут ожидаема
            return "busy"
        # ready, paused или awaiting (результат ждёт решения пользователя): сердцебиение идёт
        if self.age > HEARTBEAT_ALIVE_SEC:
            return "silent"
        return reported

    @property
    def responsive(self) -> bool:
        return self.state != "silent"


def windows_now(root: Path) -> float:
    """Текущее время по часам Windows - mtime только что записанного файла в каталоге обмена.

    Сердцебиение сеанса - mtime session.json, его ставит Windows. Часы WSL после сна машины могут
    уходить от часов Windows, поэтому сравниваем время одних и тех же часов.
    """
    root.mkdir(parents=True, exist_ok=True)
    probe = root / CLOCK_PROBE
    probe.write_text(str(time.time()), encoding="ascii")
    return probe.stat().st_mtime


def list_sessions(root: Path, prune: bool = True) -> list[Session]:
    if not root.is_dir():
        return []
    now = windows_now(root)
    sessions: list[Session] = []
    for entry in sorted(root.iterdir()):
        if not _is_session_dir(entry):
            continue
        info_file = entry / "session.json"
        try:
            mtime = info_file.stat().st_mtime
        except FileNotFoundError:
            if prune and now - _mtime(entry, now) > STALE_PRUNE_SEC:
                shutil.rmtree(entry, ignore_errors=True)
            continue
        session = Session(entry.name, entry, read_json(info_file, attempts=3) or {}, max(0.0, now - mtime))
        if prune and session.age > STALE_PRUNE_SEC:
            shutil.rmtree(entry, ignore_errors=True)
            continue
        sessions.append(session)
    return sessions


def find_session(root: Path, selector: str | None) -> Session:
    sessions = list_sessions(root)
    if selector and selector.strip():
        wanted = selector.strip().casefold()
        exact = [s for s in sessions if s.sid == wanted]
        if exact:
            return exact[0]
        matched = [s for s in sessions if wanted in _search_text(s)]
        candidates = [s for s in matched if s.responsive] or matched
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise BridgeError(f"Сеанс «{selector}» не найден.\n\n" + describe_sessions(sessions, root))
        raise BridgeError(f"Под «{selector}» подходит несколько сеансов, укажите идентификатор.\n\n"
                          + describe_sessions(candidates, root))
    responsive = [s for s in sessions if s.responsive]
    if len(responsive) == 1:
        return responsive[0]
    if not responsive:
        raise BridgeError("Нет открытых сеансов моста: откройте обработку MCPLocalBridge.epf в нужной базе 1С "
                          "(Файл - Открыть).\n\n" + describe_sessions(sessions, root))
    raise BridgeError("Открыто несколько сеансов моста, укажите session.\n\n" + describe_sessions(responsive, root))


def describe_sessions(sessions: list[Session], root: Path) -> str:
    if not sessions:
        return (f"Сеансов нет (каталог обмена {root}). Если на форме обработки выбран другой каталог обмена, "
                "MCP-серверу нужен тот же: переменная окружения MCP_1C_BRIDGE_DIR.")
    return "\n\n".join(describe_session(s) for s in sessions)


def describe_session(session: Session) -> str:
    info = session.info
    lines = [f"{session.sid}: {state_text(session)}"]
    if info.get("infobase"):
        lines.append(f"  База: {info['infobase']}")
    configuration = info.get("configurationSynonym") or info.get("configuration")
    if configuration:
        details = [part for part in (
            f"версия {info['configurationVersion']}" if info.get("configurationVersion") else "",
            f"режим совместимости {info['compatibilityMode']}" if info.get("compatibilityMode") else "",
        ) if part]
        lines.append(f"  Конфигурация: {configuration}" + (f" ({', '.join(details)})" if details else ""))
    user = info.get("userFullName") or info.get("user")
    if user:
        number = f", сеанс {info['sessionNumber']}" if info.get("sessionNumber") else ""
        lines.append(f"  Пользователь: {user}{number}")
    if info.get("platform"):
        lines.append(f"  Платформа: {info['platform']}, обработка {info.get('bridgeVersion', '?')}")
    lines.append("  Разрешено: запись в базу - {}, код на клиенте - {}".format(
        "да" if info.get("allowWrite") else "нет", "да" if info.get("allowClientCode") else "нет"))
    if info.get("mode") == "manual":
        lines.append("  Выдача результатов: с подтверждением пользователя - ответ приходит после его решения")
    return "\n".join(lines)


def state_text(session: Session) -> str:
    state = session.state
    if state == "busy":
        command = session.info.get("busyCommand") or "?"
        return f"выполняет команду {command} (без сердцебиения {session.age:.0f} с)"
    if state == "silent":
        return (f"не отвечает {session.age:.0f} с: форма закрыта без уборки, клиент упал "
                "или на экране модальное окно 1С")
    if state == "paused":
        return "приостановлен на форме обработки"
    if state == "awaiting":
        return f"результат команды {session.info.get('busyCommand') or '?'} ждёт решения пользователя"
    return f"готов (сердцебиение {session.age:.0f} с назад)"


def new_command_id() -> str:
    now = datetime.now(timezone.utc)
    return f"{now:%Y%m%dT%H%M%S}{now.microsecond // 1000:03d}-{secrets.token_hex(2)}"


def submit(session: Session, command_type: str, fields: dict[str, Any], ttl_sec: float) -> str:
    """Кладёт команду во входящие сеанса. ttlSec - срок, за который обработка должна её начать."""
    inbox = session.path / "in"
    if not inbox.is_dir():
        raise BridgeError(f"Каталог сеанса {session.sid} неполный или удалён: форма обработки закрыта?")
    command_id = new_command_id()
    command = {"protocol": PROTOCOL, "id": command_id, "type": command_type,
               "ttlSec": int(math.ceil(ttl_sec)), **fields}
    temporary = inbox / f"{command_id}.tmp"
    temporary.write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, inbox / f"{command_id}.json")
    return command_id


async def wait_result(session_path: Path, command_id: str, timeout: float) -> tuple[str, dict[str, Any] | None]:
    """Ждёт ответ обработки. Возвращает ("done", ответ), ("closed", None) или ("timeout", None)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.05
    while True:
        response = take_result(session_path, command_id)
        if response is not None:
            return "done", response
        if not session_path.exists():
            return "closed", None
        remaining = deadline - loop.time()
        if remaining <= 0:
            return "timeout", None
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, 0.3)


def cached_result(command_id: str) -> dict[str, Any] | None:
    return _results.get(command_id)


def take_result(session_path: Path, command_id: str) -> dict[str, Any] | None:
    cached = _results.get(command_id)
    if cached is not None:
        return cached
    result_file = session_path / "out" / f"{command_id}.json"
    if not result_file.exists():
        return None
    response = read_json(result_file, attempts=10)
    if not isinstance(response, dict):
        raise BridgeError(f"Файл ответа не читается как JSON: {result_file}")
    try:
        result_file.unlink()
    except OSError:
        pass
    _remember_result(command_id, response)
    return response


def withdraw(session_path: Path, command_id: str) -> bool:
    """Снимает команду, если обработка её ещё не забрала. Истина - снята и выполняться не будет."""
    try:
        (session_path / "in" / f"{command_id}.json").unlink()
    except FileNotFoundError:
        return False
    return True


def command_state(session_path: Path, command_id: str) -> str:
    if command_id in _results or (session_path / "out" / f"{command_id}.json").exists():
        return "done"
    if (session_path / "work" / f"{command_id}.json").exists():
        return "running"
    if (session_path / "in" / f"{command_id}.json").exists():
        return "queued"
    if not session_path.exists():
        return "closed"
    return "unknown"


def awaiting_approval(root: Path, session_path: Path, command_id: str) -> bool:
    """Истина, если результат команды выполнен и ждёт решения пользователя (ручной режим выдачи)."""
    current = next((s for s in list_sessions(root, prune=False) if s.path == session_path), None)
    return bool(current and current.state == "awaiting" and current.info.get("busyCommand") == command_id)


def locate_command(root: Path, command_id: str) -> Path | None:
    """Каталог сеанса, где лежит команда (если MCP перезапускался и не помнит её)."""
    if not root.is_dir():
        return None
    for entry in root.iterdir():
        if _is_session_dir(entry) and any((entry / box / f"{command_id}.json").exists()
                                          for box in ("in", "work", "out")):
            return entry
    return None


def read_json(path: Path, attempts: int = 1) -> Any:
    # session.json обработка перезаписывает на месте: можно попасть на полузаписанный или занятый файл
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt + 1 >= attempts:
                return None
            time.sleep(0.05)
    return None


_results: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _remember_result(command_id: str, response: dict[str, Any]) -> None:
    _results[command_id] = response
    while len(_results) > 100:
        _results.popitem(last=False)


def _is_session_dir(path: Path) -> bool:
    return SESSION_NAME.fullmatch(path.name) is not None and path.is_dir()


def _mtime(path: Path, default: float) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return default


def _search_text(session: Session) -> str:
    info = session.info
    parts = (info.get("infobase"), info.get("configuration"), info.get("configurationSynonym"),
             info.get("user"), info.get("userFullName"))
    return " ".join(str(part) for part in parts if part).casefold()
