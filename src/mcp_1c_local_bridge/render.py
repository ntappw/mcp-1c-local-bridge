"""Представление ответов обработки для агента: таблицы, JSON, вынос больших результатов в файл."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .metaindex import summary as index_summary
from .paths import results_dir

INLINE_LIMIT = int(os.environ.get("MCP_1C_BRIDGE_INLINE_LIMIT", "20000"))
CELL_LIMIT = 300
SYNONYMS_LIMIT = 300
RESULT_FILES_TTL_SEC = 7 * 24 * 3600
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})$")
_OWN_FRAME = re.compile(r"^\{ВнешняяОбработка\.MCPLocalBridge\w*\.")


def render_response(response: dict[str, Any], kind: str, output_format: str) -> str:
    status = response.get("status")
    comment = str(response.get("comment") or "").strip()
    parts: list[str] = []
    if comment and status != "denied":
        # Перед результатом: от большого вывода в ответе остаётся только начало (fit)
        parts.append(f"Комментарий пользователя с формы моста:\n{comment}")
    if status == "ok":
        parts.append(render_result(response.get("result"), kind, output_format))
    elif status == "rejected":
        parts.append(f"Обработка отказалась выполнять команду: {response.get('error', '')}")
    elif status == "denied" and comment:
        parts.append(f"Пользователь не разрешил выдачу результата агенту. Его пояснение с формы моста:\n{comment}")
    elif status == "denied":
        parts.append("Пользователь не разрешил выдачу результата агенту и не пояснил почему: тот же запрос "
                     "не повторяй, спроси пользователя, что не так.")
    elif status == "expired":
        parts.append("Обработка не начала команду в отведённый срок (была занята или приостановлена), "
                     "команда не выполнялась.")
    else:
        error = str(response.get("error", ""))
        if response.get("violation"):
            parts.append(f"Обход запрета записи: {error} Не повторяй команду и не продолжай работу с базой: "
                         "сообщи пользователю, что произошло, - решение за ним.")
        else:
            parts.append(f"Ошибка 1С: {error}")
        if "безопасный режим" in error.casefold():
            parts.append("Код агента выполняется в безопасном режиме: файлы сервера, интернет, COM, внешние "
                         "компоненты и привилегированный режим в нём недоступны. Если операция действительно нужна, "
                         "попроси пользователя выключить «Безопасный режим» на форме моста.")
        details = agent_details(str(response.get("errorDetails") or ""), error)
        if details:
            parts.append(f"Подробно:\n{details}")
    messages = response.get("messages") or []
    if messages:
        parts.append("Сообщения пользователю:\n" + "\n".join(f"- {text}" for text in messages))
    for warning in response.get("warnings") or []:
        parts.append(f"Внимание: {warning}")
    footer = f"[{response.get('durationMs', '?')} мс, команда {response.get('id', '?')}"
    transaction = response.get("transaction")
    if transaction == "committed":
        footer += ", транзакция зафиксирована - изменения сохранены"
    elif transaction == "none":
        footer += ", без транзакции моста - записанное кодом сохранено, в том числе до ошибки"
    elif response.get("rolledBack"):
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
    if kind == "index" and isinstance(result, dict):
        return index_summary(result)
    if kind.startswith("test.") and isinstance(result, dict):
        return render_test(result, kind.partition(".")[2])
    if kind.startswith("file.") and isinstance(result, dict):
        return render_file(result, kind.partition(".")[2])
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


def render_test(result: dict[str, Any], action: str) -> str:
    if action == "connect":
        how = {"already": "уже был подключён", "connected": "подключение к запущенному клиенту",
               "launched": "клиент запущен этим вызовом"}.get(str(result.get("connection")), "")
        address = result.get("address") or "?"
        return (f"Клиент тестирования подключён: {address}" + (f", {how}" if how else "") + ".\n"
                + _windows_line(result.get("windows") or []))
    if action == "close":
        if result.get("closed"):
            return "Клиент тестирования закрыт."
        return str(result.get("note") or "Клиент тестирования не закрыт.")
    if action == "steps":
        return _render_steps(result)
    return _render_snapshot(result)


def _windows_line(windows: list[dict[str, Any]]) -> str:
    titles = []
    for window in windows:
        marks = [mark for key, mark in (("main", "основное"), ("active", "активное")) if window.get(key)]
        titles.append(f"«{window.get('title', '')}»" + (f" ({', '.join(marks)})" if marks else ""))
    return "Окна: " + (", ".join(titles) if titles else "нет")


def _render_snapshot(result: dict[str, Any]) -> str:
    lines = [f"Окно «{result.get('window', '')}»"]
    table = result.get("table")
    if isinstance(table, dict):
        lines.append(_render_test_table(table))
    else:
        for node in result.get("items") or []:
            _append_node(lines, node, 0)
        if not result.get("items"):
            lines.append("  (в окне нет форм и элементов)")
    if result.get("truncated"):
        lines.append("...снимок неполный: истекло время, отведённое на обход (budget_sec). Сузьте снимок: "
                     "другое окно, table вместо всей формы, без details и hidden.")
    messages = result.get("messages") or []
    if messages:
        lines.append("Сообщения пользователю в окнах клиента:\n" + "\n".join(f"- {text}" for text in messages))
    if result.get("clientError"):
        lines.append(f"Ошибка в клиенте тестирования: {result['clientError']}")
    lines.append(_windows_line(result.get("windows") or []))
    return "\n".join(lines)


def _append_node(lines: list[str], node: dict[str, Any], level: int) -> None:
    kind = str(node.get("t", ""))
    name = str(node.get("name") or "")
    title = str(node.get("title") or "")
    indent = "  " * (level + 1)
    caption = name + (f" «{_one_line(title)}»" if title and title != name else "")
    if kind == "form":
        text = f"Форма {caption}".rstrip()
        marks = [mark for key, mark in (("modified", "изменена"), ("readOnly", "только просмотр")) if node.get(key)]
    else:
        label = {"field": "", "button": "[кнопка] ", "decoration": "[надпись] ", "table": "[таблица] ",
                 "group": "[группа] "}.get(kind, f"[{kind}] ")
        text = label + (caption or "(без имени)")
        if kind == "field":
            if node.get("kind"):
                text += f" ({node['kind']})"
            if "value" in node:
                text += f" = {json.dumps(node['value'], ensure_ascii=False)}"
        marks = [mark for key, mark in (("hidden", "скрыт"), ("disabled", "недоступен"),
                                        ("readOnly", "только просмотр")) if node.get(key)]
    lines.append(indent + text + (f" [{', '.join(marks)}]" if marks else ""))
    for child in node.get("items") or []:
        _append_node(lines, child, level + 1)
    if kind == "table" and isinstance(node.get("row"), list):
        cells = [f"{column}={json.dumps(value, ensure_ascii=False)}"
                 for column, value in zip(_table_columns(node), node["row"]) if value not in (None, "")]
        lines.append(indent + "  текущая строка: " + ("; ".join(cells) if cells else "(пустые ячейки)"))


def _table_columns(node: dict[str, Any]) -> list[str]:
    """Колонки таблицы в порядке обхода: так же обработка складывает ячейки текущей строки."""
    columns: list[str] = []
    for child in node.get("items") or []:
        if child.get("t") == "field" and child.get("name") and not child.get("hidden"):
            columns.append(str(child["name"]))
        elif child.get("t") != "table" and not child.get("hidden"):
            columns.extend(_table_columns(child))
    return columns


def _render_test_table(table: dict[str, Any]) -> str:
    columns = [str(name) for name in table.get("columns") or []]
    titles = [str(title) for title in table.get("titles") or []]
    rows = table.get("rows") or []
    summary = f"Таблица {table.get('name', '')}: строк прочитано {len(rows)}"
    if table.get("truncated"):
        summary += " (дальше есть ещё: увеличьте rows)"
    lines = [summary]
    if columns:
        heads = [name + (f" «{title}»" if title and title != name else "")
                 for name, title in zip(columns, titles + [""] * len(columns))]
        lines += ["| № | " + " | ".join(heads) + " |", "|" + " --- |" * (len(columns) + 1)]
        for number, row in enumerate(rows, 1):
            lines.append(f"| {number} | " + " | ".join(cell_text("" if value is None else value) for value in row)
                         + " |")
    if table.get("note"):
        lines.append(str(table["note"]))
    return "\n".join(lines)


def _render_steps(result: dict[str, Any]) -> str:
    steps = result.get("steps") or []
    total = result.get("total", len(steps))
    failed = result.get("failed", 0)
    lines = [f"Шаги: выполнено {len(steps)} из {total}, с ошибкой {failed}"]
    earlier = result.get("earlierMessages") or []
    if earlier:
        lines.append("Сообщения, висевшие в окнах до шагов (панели закрыты):\n"
                     + "\n".join(f"- {text}" for text in earlier))
    window = None
    for number, step in enumerate(steps, 1):
        text = f"{number}. {'+' if step.get('ok') else 'ОШИБКА'} {step.get('action', '')}"
        if step.get("target"):
            text += f" {step['target']}"
        if "value" in step:
            text += f" = {json.dumps(step['value'], ensure_ascii=False)}"
        if step.get("error"):
            text += f": {_one_line(str(step['error']))}"
        # Окно показывается, когда оно сменилось: открылась форма, вопрос или сообщение об ошибке
        if step.get("window") != window:
            window = step.get("window")
            text += f"  -> окно «{window}»"
        lines.append(text)
        for message in step.get("messages") or []:
            lines.append(f"   сообщение пользователю: {_one_line(str(message))}")
        if step.get("clientError"):
            lines.append(f"   ошибка в клиенте тестирования: {_one_line(str(step['clientError']))}")
    if len(steps) < total:
        lines.append(f"Шаги {len(steps) + 1}-{total} не выполнялись: прогон остановлен на ошибке.")
    return "\n".join(lines)


def render_file(result: dict[str, Any], action: str) -> str:
    size = result.get("size", "?")
    if action == "load":
        address = result.get("address", "")
        return (f"Файл {result.get('path', '')} ({size} байт) помещён во временное хранилище.\n"
                f"Адрес: {address}\n"
                f"В серверном коде: ПолучитьИзВременногоХранилища(\"{address}\") - двоичные данные. "
                "Адрес действует, пока открыта форма моста.")
    kinds = {"spreadsheet": "табличный документ", "binary": "двоичные данные", "text": "текст"}
    return (f"Файл записан: {result.get('path', '')} ({size} байт, "
            f"{kinds.get(str(result.get('kind')), result.get('kind'))})")


def _one_line(text: str) -> str:
    return " ".join(text.split())


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
