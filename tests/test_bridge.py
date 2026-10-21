"""Проверки MCP-стороны моста с имитацией обработки 1С в отдельном потоке."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_1c_local_bridge import exchange, server
from mcp_1c_local_bridge.render import agent_details


@pytest.fixture
def root(tmp_path, monkeypatch):
    exchange_dir = tmp_path / "exchange"
    monkeypatch.setenv("MCP_1C_BRIDGE_DIR", str(exchange_dir))
    monkeypatch.setenv("MCP_1C_BRIDGE_RESULTS", str(tmp_path / "results"))
    return exchange_dir


def make_session(root: Path, sid: str = "abcd1234", **info) -> Path:
    path = root / sid
    for box in ("in", "work", "out"):
        (path / box).mkdir(parents=True)
    data = {"protocol": 1, "infobase": 'Srvr="srv";Ref="base_II";', "user": "tester",
            "configuration": "Тест", "state": "ready", **info}
    (path / "session.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class FakeBridge(threading.Thread):
    """Имитация формы обработки: забирает команды из in и отвечает функцией handler."""

    def __init__(self, session_path: Path, handler=None, claim_only: bool = False, on_claim=None):
        super().__init__(daemon=True)
        self.path = session_path
        self.handler = handler or (lambda command: {"status": "ok", "result": None})
        self.claim_only = claim_only
        self.on_claim = on_claim
        self.commands: list[dict] = []
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            for incoming in sorted((self.path / "in").glob("*.json")):
                claimed = self.path / "work" / incoming.name
                try:
                    os.replace(incoming, claimed)
                except FileNotFoundError:
                    continue
                command = json.loads(claimed.read_text(encoding="utf-8"))
                self.commands.append(command)
                if self.on_claim:
                    self.on_claim(command)
                if self.claim_only:
                    continue
                response = {"protocol": 1, "id": command["id"], "durationMs": 1, **self.handler(command)}
                temporary = self.path / "out" / f"{command['id']}.tmp"
                temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, self.path / "out" / f"{command['id']}.json")
                claimed.unlink()
            time.sleep(0.02)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        self.join(timeout=2)


def query_table(rows):
    return {"results": [{
        "columns": [{"name": "Ссылка", "types": ["Справочник.Контрагенты"]}, {"name": "Сумма", "types": ["Число"]},
                    {"name": "Дата", "types": ["Дата"]}],
        "rows": rows, "total": len(rows), "truncated": False}]}


def test_query_renders_table_and_normalizes_params(root):
    path = make_session(root)
    rows = [[{"_type": "Справочник.Контрагенты", "_uuid": "u-1", "_presentation": "ООО «Ромашка»"},
             1500.0, "2026-10-21T00:00:00"]]
    with FakeBridge(path, lambda command: {"status": "ok", "result": query_table(rows)}) as bridge:
        text = asyncio.run(server.query("ВЫБРАТЬ 1", params={"Дата": {"_date": "2026-01-01"}, "Список": [1, 2]}))
    assert "| ООО «Ромашка» | 1500 | 2026-10-21 00:00:00 |" in text
    command = bridge.commands[0]
    assert command["type"] == "query"
    assert command["params"] == {"Дата": {"_date": "2026-01-01T00:00:00"}, "Список": [1, 2]}
    assert command["ttlSec"] == 60


def test_command_not_taken_in_time_is_withdrawn(root):
    path = make_session(root)
    with pytest.raises(ToolError, match="снята"):
        asyncio.run(server.execute("Результат = 1;", timeout=1))
    assert list((path / "in").iterdir()) == []


def test_long_command_returns_id_and_result_later(root):
    path = make_session(root)
    with FakeBridge(path, claim_only=True) as bridge:
        text = asyncio.run(server.execute("Результат = 1;", timeout=1))
        assert "выполняется дольше" in text
        command_id = bridge.commands[0]["id"]
    response = {"protocol": 1, "id": command_id, "status": "ok", "result": {"Ответ": 42}, "rolledBack": True}
    (path / "out" / f"{command_id}.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    later = asyncio.run(server.result(command_id, wait=1))
    assert '"Ответ": 42' in later
    assert "транзакция откатана" in later


def test_session_selection(root):
    make_session(root, "aaaa0001", infobase='Srvr="srv";Ref="Trade_II";')
    make_session(root, "bbbb0002", infobase='Srvr="srv";Ref="Accounting_AI";')
    with pytest.raises(exchange.BridgeError, match="несколько"):
        exchange.find_session(root, None)
    assert exchange.find_session(root, "accounting_ai").sid == "bbbb0002"
    assert exchange.find_session(root, "aaaa0001").sid == "aaaa0001"


def test_silent_session_is_not_picked(root):
    path = make_session(root)
    old = time.time() - 120
    os.utime(path / "session.json", (old, old))
    with pytest.raises(exchange.BridgeError, match="Нет открытых сеансов"):
        exchange.find_session(root, None)
    assert "не отвечает" in asyncio.run(server.sessions())


def test_busy_session_stays_selectable(root):
    path = make_session(root, state="busy", busyCommand="x")
    old = time.time() - 120
    os.utime(path / "session.json", (old, old))
    assert exchange.find_session(root, None).state == "busy"


def test_error_and_rejection_rendering(root):
    path = make_session(root)

    def handler(command):
        if command["type"] == "eval":
            return {"status": "rejected", "error": "Запись в базу не разрешена"}
        return {"status": "error", "error": "Поле не найдено", "errorDetails": "{(1,8)}: Поле не найдено",
                "messages": ["подсказка"]}

    with FakeBridge(path, handler):
        rejected = asyncio.run(server.eval_expression("1", allow_write=True))
        failed = asyncio.run(server.execute("Результат = Х;"))
    assert "отказалась" in rejected
    assert "Ошибка 1С: Поле не найдено" in failed
    assert "{(1,8)}" in failed
    assert "- подсказка" in failed


def test_error_details_drop_bridge_stack():
    details = ("Деление на 0\n{(2)}:Результат = 1 / Делитель;\n"
               "{ВнешняяОбработка.MCPLocalBridge.МодульОбъекта(152)}:Выполнить(Код);")
    assert agent_details(details, "Деление на 0") == "Деление на 0\n{(2)}:Результат = 1 / Делитель;"
    query = ("Ошибка при вызове метода контекста (ВыполнитьПакет)\n"
             "{ВнешняяОбработка.MCPLocalBridge.МодульОбъекта(90)}:Для Каждого\n\n"
             "по причине:\n{(1, 16)}: Поле не найдено")
    assert agent_details(query, "Ошибка при вызове метода контекста (ВыполнитьПакет)\n{(1, 16)}: Поле не найдено") == ""


def test_large_output_goes_to_file(root, tmp_path):
    path = make_session(root)
    rows = [[{"_presentation": f"Контрагент {index}"}, index, "2026-10-21T00:00:00"] for index in range(3000)]
    with FakeBridge(path, lambda command: {"status": "ok", "result": query_table(rows)}):
        text = asyncio.run(server.query("ВЫБРАТЬ", row_limit=5000))
    assert "не поместился" in text
    saved = list((tmp_path / "results").glob("*.md"))
    assert saved and "Контрагент 2999" in saved[0].read_text(encoding="utf-8")


def test_metadata_composition_rendering(root):
    path = make_session(root)
    composition = {"configuration": {"name": "БП", "synonym": "Бухгалтерия", "version": "3.0"},
                   "collections": [{"name": "Справочники", "objects": [
                       {"name": "Контрагенты", "synonym": "Контрагенты"},
                       {"name": "Доработки_Перевозчики", "synonym": "Перевозчики", "extension": "Доработки"}]}]}
    with FakeBridge(path, lambda command: {"status": "ok", "result": composition}):
        text = asyncio.run(server.metadata(filter="а"))
    assert "Справочники (2): Контрагенты, Доработки_Перевозчики «Перевозчики» [расширение Доработки]" in text


def test_manual_mode_awaiting_then_denied(root):
    path = make_session(root, mode="manual")

    def await_decision(command):
        # Ручной режим: обработка выполнила команду, а результат ждёт решения пользователя
        info = json.loads((path / "session.json").read_text(encoding="utf-8"))
        info.update(state="awaiting", busyCommand=command["id"])
        (path / "session.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")

    with FakeBridge(path, claim_only=True, on_claim=await_decision) as bridge:
        text = asyncio.run(server.query("ВЫБРАТЬ 1", timeout=1))
        command_id = bridge.commands[0]["id"]
    assert "ждёт решения пользователя" in text
    assert "ждёт решения пользователя" in asyncio.run(server.result(command_id, wait=0))
    assert "с подтверждением" in asyncio.run(server.sessions())

    denied = {"protocol": 1, "id": command_id, "status": "denied", "error": "не разрешил", "durationMs": 5}
    (path / "out" / f"{command_id}.json").write_text(json.dumps(denied, ensure_ascii=False), encoding="utf-8")
    assert "Пользователь не разрешил выдачу" in asyncio.run(server.result(command_id, wait=1))
