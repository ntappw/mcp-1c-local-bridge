"""Представление ответов обработки для агента: таблицы, JSON, вынос больших результатов в файл."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .paths import results_dir

INLINE_LIMIT = int(os.environ.get("MCP_1C_BRIDGE_INLINE_LIMIT", "20000"))
CELL_LIMIT = 300
SYNONYMS_LIMIT = 300
RESULT_FILES_TTL_SEC = 7 * 24 * 3600
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})$")
_OWN_FRAME = re.compile(r"^\{ВнешняяОбработка\.MCPLocalBridge\.")


def render_response(response: dict[str, Any], kind: str, output_format: str) -> str:
    status = response.get("status")
    parts: list[str] = []
    if status == "ok":
        parts.append(render_result(response.get("result"), kind, output_format))
    elif status == "rejected":
        parts.append(f"Обработка отказалась выполнять команду: {response.get('error', '')}")
    elif status == "denied":
        parts.append("Пользователь не разрешил выдачу результата агенту.")
    elif status == "expired":
        parts.append("Обработка не начала команду в отведённый срок (была занята или приостановлена), "
                     "команда не выполнялась.")
    else:
        error = str(response.get("error", ""))
        parts.append(f"Ошибка 1С: {error}")
        details = agent_details(str(response.get("errorDetails") or ""), error)
        if details:
            parts.append(f"Подробно:\n{details}")
    messages = response.get("messages") or []
    if messages:
        parts.append("Сообщения пользователю:\n" + "\n".join(f"- {text}" for text in messages))
    for warning in response.get("warnings") or []:
        parts.append(f"Внимание: {warning}")
    footer = f"[{response.get('durationMs', '?')} мс, команда {response.get('id', '?')}"
    if response.get("rolledBack"):
        footer += ", транзакция откатана - изменения в базе не сохранены"
    parts.append(footer + "]")
    return "\n\n".join(part for part in parts if part)


def agent_details(details: str, error: str) -> str:
    """Подробности ошибки без стека самой обработки; пусто, если к тексту ошибки они ничего не добавляют."""
    lines = [line for line in details.splitlines() if not _OWN_FRAME.match(line.strip())]
    known = {line.strip() for line in error.splitlines() if line.strip()}
    news = [line for line in lines if line.strip() and line.strip() not in known and line.strip() != "по причине:"]
    return "\n".join(lines).strip() if news else ""


def render_result(result: Any, kind: str, output_format: str) -> str:
    if output_format == "json":
        return to_json(result)
    if kind == "query" or (isinstance(result, dict) and isinstance(result.get("results"), list)):
        return render_query(result)
    if kind == "metadata" and isinstance(result, dict):
        return render_metadata(result)
    if is_table(result):
        return render_table(result)
    return to_json(result)


def render_query(result: Any) -> str:
    tables = result.get("results") if isinstance(result, dict) else None
    if not isinstance(tables, list):
        return to_json(result)
    blocks = []
    for index, table in enumerate(tables, 1):
        if not is_table(table):
            continue
        title = f"Результат {index} из {len(tables)}: " if len(tables) > 1 else ""
        blocks.append(render_table(table, title))
    return "\n\n".join(blocks) or "Запрос не вернул результатов."


def render_table(table: dict[str, Any], title: str = "") -> str:
    columns = [str(column.get("name", "")) for column in table.get("columns", [])]
    rows = table.get("rows") or []
    total = table.get("total", len(rows))
    summary = f"{title}строк {total}"
    if table.get("truncated"):
        summary += f", показаны первые {len(rows)} (ограничение row_limit)"
    if not rows:
        return summary + (f"; колонки: {', '.join(columns)}" if columns else "")
    lines = [summary, "| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for row in rows:
        cells = row if isinstance(row, list) else [row]
        lines.append("| " + " | ".join(cell_text(value) for value in cells) + " |")
    return "\n".join(lines)


def cell_text(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "Истина" if value else "Ложь"
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        match = _DATE.match(value)
        if match:
            text = "" if value.startswith("0001-01-01") else f"{match.group(1)} {match.group(2)}"
        else:
            text = value
    elif isinstance(value, dict) and "_presentation" in value:
        text = str(value.get("_presentation") or "")
    elif is_table(value):
        text = f"<{value.get('_type', 'таблица')}: строк {value.get('total', len(value.get('rows') or []))}>"
    else:
        text = json.dumps(value, ensure_ascii=False)
    if len(text) > CELL_LIMIT:
        text = text[:CELL_LIMIT] + "...(обрезано)"
    return text.replace("\r\n", "\n").replace("\n", "\\n").replace("|", "\\|")


def render_metadata(result: dict[str, Any]) -> str:
    if isinstance(result.get("collections"), list):
        return _render_composition(result)
    return _render_object(result)


def _render_composition(result: dict[str, Any]) -> str:
    collections = result.get("collections") or []
    configuration = result.get("configuration") or {}
    lines = []
    if configuration:
        lines.append(f"Конфигурация {configuration.get('name', '')} «{configuration.get('synonym', '')}», "
                     f"версия {configuration.get('version', '')}")
    count = sum(len(collection.get("objects") or []) for collection in collections)
    with_synonyms = count <= SYNONYMS_LIMIT
    for collection in collections:
        objects = collection.get("objects") or []
        names = [_object_title(item, with_synonyms) for item in objects]
        lines.append(f"{collection.get('name')} ({len(objects)}): " + ", ".join(names))
    if not collections:
        lines.append("Объектов не найдено.")
    return "\n".join(lines)


def _render_object(result: dict[str, Any]) -> str:
    lines = [_object_title({**result, "name": result.get("fullName") or result.get("name", "")}, True)]
    if result.get("comment"):
        lines.append(f"Комментарий: {result['comment']}")
    if result.get("types"):
        lines.append(f"Тип: {', '.join(result['types'])}")
    if result.get("standardAttributes"):
        names = ", ".join(item.get("name", "") for item in result["standardAttributes"])
        lines.append(f"Стандартные реквизиты: {names}")
    for key, title in (("dimensions", "Измерения"), ("resources", "Ресурсы"), ("attributes", "Реквизиты")):
        if result.get(key):
            lines.append(f"{title}:\n{_fields_table(result[key])}")
    if result.get("values"):
        lines.append("Значения: " + ", ".join(_object_title(item, True) for item in result["values"]))
    for section in result.get("tabularSections") or []:
        fields = _fields_table(section.get("attributes") or [])
        lines.append(f"Табличная часть {_object_title(section, True)}:\n{fields}")
    for key, title in (("registerRecords", "Движения"), ("forms", "Формы"), ("commands", "Команды"),
                       ("templates", "Макеты")):
        if result.get(key):
            lines.append(f"{title}: {', '.join(result[key])}")
    return "\n\n".join(lines)


def _fields_table(fields: list[dict[str, Any]]) -> str:
    if not fields:
        return "(нет)"
    lines = ["| Имя | Синоним | Тип |", "| --- | --- | --- |"]
    for item in fields:
        lines.append("| {} | {} | {} |".format(
            cell_text(item.get("name", "")), cell_text(item.get("synonym", "")),
            cell_text(", ".join(item.get("types") or []))))
    return "\n".join(lines)


def _object_title(item: dict[str, Any], with_synonym: bool) -> str:
    title = str(item.get("name", ""))
    if with_synonym and item.get("synonym") and item.get("synonym") != item.get("name"):
        title += f" «{item['synonym']}»"
    if item.get("extension"):
        title += f" [расширение {item['extension']}]"
    return title


def is_table(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("columns"), list)
            and isinstance(value.get("rows"), list) and all(isinstance(row, list) for row in value["rows"]))


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def fit(text: str, response: dict[str, Any], command_id: str) -> str:
    """Большой вывод кладёт в файлы и возвращает начало с путями к ним."""
    if len(text) <= INLINE_LIMIT:
        return text
    folder = results_dir()
    folder.mkdir(parents=True, exist_ok=True)
    _prune_result_files(folder)
    rendered_file = folder / f"{command_id}.md"
    raw_file = folder / f"{command_id}.json"
    rendered_file.write_text(text, encoding="utf-8")
    raw_file.write_text(json.dumps(response, ensure_ascii=False, indent=1), encoding="utf-8")
    head = text[: INLINE_LIMIT // 4]
    if "\n" in head:
        head = head[: head.rindex("\n")]
    return (f"{head}\n\n...вывод ({len(text)} символов) не поместился в ответ.\n"
            f"Полностью: {rendered_file}\nОтвет обработки в JSON: {raw_file}")


def _prune_result_files(folder) -> None:
    border = time.time() - RESULT_FILES_TTL_SEC
    for path in folder.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < border:
                path.unlink()
        except OSError:
            pass
