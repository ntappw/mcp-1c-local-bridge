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


def test_ordinary_application_in_sessions(root):
    make_session(root, platform="8.3.24.1691", bridgeVersion="1.3.0", application="ordinary")
    described = asyncio.run(server.sessions())
    assert "Платформа: 8.3.24.1691, обработка 1.3.0, обычное приложение" in described
    assert "агенту сообщения не возвращаются" in described and "без транзакции и отката" in described


def test_silent_session_is_not_picked(root):
    path = make_session(root)
    old = time.time() - 120
    os.utime(path / "session.json", (old, old))
    with pytest.raises(exchange.BridgeError, match="Нет открытых сеансов"):
        exchange.find_session(root, None)
    assert "не отвечает" in asyncio.run(server.sessions())


def test_only_session_folders_are_pruned(root):
    # Каталог обмена может быть общим (MCP_1C_BRIDGE_DIR): чужие папки в нём не трогаются
    stale = make_session(root, "cccc0003")
    orphan = root / "dddd0004"
    orphan.mkdir()
    foreign = root / "Документы"
    (foreign / "Архив").mkdir(parents=True)
    old = time.time() - 3 * 24 * 3600
    for path in (stale / "session.json", orphan, foreign):
        os.utime(path, (old, old))
    assert exchange.list_sessions(root) == []
    assert not stale.exists() and not orphan.exists()
    assert (foreign / "Архив").is_dir()
    assert "MCP_1C_BRIDGE_DIR" in asyncio.run(server.sessions())


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


def test_write_transaction_request_and_outcome(root):
    path = make_session(root)
    outcomes = iter([
        {"status": "ok", "result": 1, "rolledBack": False, "transaction": "committed"},
        {"status": "error", "error": "Деление на 0", "rolledBack": True, "transaction": "rolledBack"},
        {"status": "ok", "result": 1, "rolledBack": False, "transaction": "none"},
    ])
    with FakeBridge(path, lambda command: next(outcomes)) as bridge:
        committed = asyncio.run(server.execute("Результат = 1;", allow_write=True))
        failed = asyncio.run(server.execute("Результат = 1 / 0;", allow_write=True))
        plain = asyncio.run(server.eval_expression("1", allow_write=True, transaction=False))
    # Запись по умолчанию просит транзакцию; transaction=false уходит обработке как есть
    assert [(c["allowWrite"], c["transaction"]) for c in bridge.commands] == [(True, True), (True, True), (True, False)]
    assert "транзакция зафиксирована - изменения сохранены" in committed
    assert "Деление на 0" in failed and "транзакция откатана" in failed
    assert "без транзакции моста" in plain


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


def test_manual_mode_awaiting_then_denied_with_comment(root):
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

    denied = {"comment": "Нужен только октябрь,\nбез ФИО", "protocol": 1, "id": command_id, "status": "denied",
              "error": "не разрешил", "durationMs": 5}
    (path / "out" / f"{command_id}.json").write_text(json.dumps(denied, ensure_ascii=False), encoding="utf-8")
    later = asyncio.run(server.result(command_id, wait=1))
    assert "Пользователь не разрешил выдачу" in later
    assert "Его пояснение с формы моста:\nНужен только октябрь,\nбез ФИО" in later


def test_denial_without_comment_and_comment_before_large_result(root):
    path = make_session(root)
    rows = [[{"_presentation": f"Контрагент {index}"}, index, "2026-10-21T00:00:00"] for index in range(3000)]
    answers = iter([{"status": "denied", "error": "не разрешил"},
                    {"comment": "Суммы в тысячах рублей", "status": "ok", "result": query_table(rows)}])
    with FakeBridge(path, lambda command: next(answers)):
        denied = asyncio.run(server.query("ВЫБРАТЬ 1"))
        approved = asyncio.run(server.query("ВЫБРАТЬ", row_limit=5000))
    assert "не пояснил почему" in denied and "не повторяй" in denied
    # Большой вывод уходит в файл, в ответе остаётся начало - комментарий в нём
    assert "не поместился" in approved
    assert approved.startswith("Комментарий пользователя с формы моста:\nСуммы в тысячах рублей")


SAMPLE_INDEX = {
    "format": 1, "fingerprint": "f1", "built": "2026-11-03T10:15:00",
    "configuration": {"name": "Тест", "version": "1.0"}, "extensions": [], "objects": 11, "fields": 7,
    "parts": [
        {"collection": "Справочники", "types": [["Строка(12)"], ["Справочник.Контрагенты"], ["Строка(150)"]],
         "objects": [
             {"full": "Справочник.Контрагенты", "name": "Контрагенты",
              "attributes": [["ИНН", "", 0], ["ГоловнойКонтрагент", "Головной контрагент", 1]],
              "tables": [{"name": "КонтактнаяИнформация", "attributes": [["Представление", "", 2]]}],
              "predefined": ["Основной"], "forms": ["ФормаЭлемента"]},
             {"full": "Справочник.ИнновационныеПроекты", "name": "ИнновационныеПроекты",
              "synonym": "Инновационные проекты", "attributes": [["Шифр", "", 2]]},
             {"full": "Справочник.ДоговорыКонтрагентов", "name": "ДоговорыКонтрагентов",
              "owners": ["Справочник.Контрагенты"]},
             {"full": "Справочник.Сломанный", "error": "Поле объекта не обнаружено (Владельцы)"}]},
        {"collection": "Документы",
         "types": [["Справочник.Контрагенты"], ["Справочник.Контрагенты", "Справочник.ДоговорыКонтрагентов"]],
         "objects": [{"full": "Документ.Реализация", "name": "Реализация", "synonym": "Реализация товаров",
                      "attributes": [["Контрагент", "", 0], ["ВладелецФайла", "", 1, "Доработки"]],
                      "records": ["РегистрНакопления.Взаиморасчеты"]}]},
        {"collection": "ОпределяемыеТипы", "types": [["Справочник.Контрагенты", "Справочник.ДоговорыКонтрагентов"]],
         "objects": [{"full": "ОпределяемыйТип.ВладелецФайлов", "name": "ВладелецФайлов", "type": 0}]},
        {"collection": "РегистрыНакопления", "types": [["Справочник.Контрагенты"], ["Число(15,2)"]],
         "objects": [{"full": "РегистрНакопления.Взаиморасчеты", "name": "Взаиморасчеты", "registerKind": "Остатки",
                      "dimensions": [["Контрагент", "", 0]], "resources": [["Сумма", "", 1]]}]},
        {"collection": "ПодпискиНаСобытия", "types": [["Справочник.Контрагенты"]],
         "objects": [{"full": "ПодпискаНаСобытие.ПроверкаИНН", "name": "ПроверкаИНН", "source": 0,
                      "event": "ПередЗаписью", "handler": "КонтрагентыСервер.ПроверитьИНН"}]},
        {"collection": "ПланыОбмена", "types": [],
         "objects": [{"full": "ПланОбмена.СБП", "name": "СБП", "exchangeContent": [["Справочник.Контрагенты", True]]}]},
        {"collection": "Подсистемы", "types": [],
         "objects": [{"full": "Подсистема.Продажи", "name": "Продажи",
                      "content": ["Справочник.Контрагенты", "Документ.Реализация"]}]},
        {"collection": "РегламентныеЗадания", "types": [],
         "objects": [{"full": "РегламентноеЗадание.ОбновлениеКонтрагентов", "name": "ОбновлениеКонтрагентов",
                      "method": "КонтрагентыСервер.Обновить", "isPredefined": True, "use": False}]},
        {"collection": "ОбщиеМодули", "types": [],
         "objects": [{"full": "ОбщийМодуль.КонтрагентыСервер", "name": "КонтрагентыСервер", "flags": ["Сервер"],
                      "reuse": "Не использовать"}]},
    ]}


@pytest.fixture(autouse=True)
def clean_index_cache():
    # Индекс MCP помнит по идентификатору сеанса, а в тестах он у всех сеансов один
    for cache in (server._indexes, server._index_commands, server._index_paths):
        cache.clear()


def index_handler(path: Path, index=None):
    index = index or SAMPLE_INDEX

    def handler(command):
        if command["type"] != "index":
            return {"status": "ok", "result": None}
        # Обработка пишет файлы в UTF-8 с BOM
        (path / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8-sig")
        return {"status": "ok", "result": {"file": "index.json", "source": "built", "fingerprint": index["fingerprint"],
                                           "built": index["built"], "objects": index["objects"],
                                           "fields": index["fields"]}}

    return handler


def test_find_searches_index_from_memory(root):
    path = make_session(root)
    with FakeBridge(path, index_handler(path)) as bridge:
        found = asyncio.run(server.find("инн"))
        by_owner = asyncio.run(server.find("контрагенты инн"))
        only_fields = asyncio.run(server.find("ИНН", kind="Реквизиты"))
        refreshed = asyncio.run(server.find("договор", refresh=True))
    assert "Индекс метаданных построен заново (построен 2026-11-03 10:15 UTC): объектов 11" in found
    assert "Не описаны в индексе (1): Справочник.Сломанный" in found
    assert "Справочник.Контрагенты.Реквизит.ИНН: Строка(12)" in found
    assert "ПодпискаНаСобытие.ПроверкаИНН (ПередЗаписью -> КонтрагентыСервер.ПроверитьИНН)" in found
    # Слово из имени владельца сужает поиск, но одно поле не находит: полей «инновационных проектов» нет;
    # синоним, повторяющий имя, не выводится
    assert "Справочник.ИнновационныеПроекты" in found.splitlines()
    assert "ИнновационныеПроекты.Реквизит" not in found
    assert by_owner.splitlines()[1] == "Справочник.Контрагенты.Реквизит.ИНН: Строка(12)"
    assert "ПодпискаНаСобытие" not in only_fields
    assert "Справочник.ДоговорыКонтрагентов" in refreshed
    # Повторные поиски - по индексу в памяти MCP; refresh - новая команда с флагом
    assert [(command["type"], command["refresh"]) for command in bridge.commands] == [("index", False), ("index", True)]


def test_usages_shows_reverse_links(root):
    path = make_session(root)
    with FakeBridge(path, index_handler(path)):
        links = asyncio.run(server.usages("СправочникСсылка.Контрагенты"))
        recorders = asyncio.run(server.usages("РегистрНакопления.Взаиморасчеты"))
        module = asyncio.run(server.usages("ОбщийМодуль.КонтрагентыСервер"))
        missing = asyncio.run(server.usages("Справочник.Нет"))
    assert "Связи Справочник.Контрагенты по индексу метаданных:" in links
    assert "- Документ.Реализация.Реквизит.Контрагент: Справочник.Контрагенты" in links
    assert "- РегистрНакопления.Взаиморасчеты.Измерение.Контрагент: Справочник.Контрагенты" in links
    assert "Поля типа ОпределяемыйТип.ВладелецФайлов, куда входит этот тип (1):" in links
    assert "- Документ.Реализация.Реквизит.ВладелецФайла" in links
    assert "- ОпределяемыйТип.ВладелецФайлов: Справочник.Контрагенты, Справочник.ДоговорыКонтрагентов" in links
    assert "- ПодпискаНаСобытие.ПроверкаИНН (ПередЗаписью -> КонтрагентыСервер.ПроверитьИНН)" in links
    assert "- ПланОбмена.СБП" in links and "- Подсистема.Продажи" in links
    assert "Подчинённые справочники (1):\n- Справочник.ДоговорыКонтрагентов" in links
    assert "пишут в этот регистр (1):\n- Документ.Реализация" in recorders
    assert "вызывающие методы модуля (2):" in module and "- ПодпискаНаСобытие.ПроверкаИНН" in module
    assert "- РегламентноеЗадание.ОбновлениеКонтрагентов (метод КонтрагентыСервер.Обновить, выключено)" in module
    assert "такого объекта в индексе нет" in missing


def test_long_index_build_is_awaited_not_restarted(root):
    path = make_session(root)
    with FakeBridge(path, claim_only=True) as bridge:
        first = asyncio.run(server.find("инн", timeout=1))
        command = bridge.commands[0]
        response = {"protocol": 1, "id": command["id"], "durationMs": 1, **index_handler(path)(command)}
        (path / "out" / f"{command['id']}.json").write_text(json.dumps(response, ensure_ascii=False),
                                                           encoding="utf-8")
        second = asyncio.run(server.find("инн", timeout=5))
    assert "строится" in first and "заново не начнётся" in first
    assert len(bridge.commands) == 1
    assert "Справочник.Контрагенты.Реквизит.ИНН" in second


def test_result_loads_index_built_after_timeout(root):
    path = make_session(root)
    with FakeBridge(path, claim_only=True) as bridge:
        asyncio.run(server.usages("Справочник.Контрагенты", timeout=1))
        command = bridge.commands[0]
    response = {"protocol": 1, "id": command["id"], "durationMs": 1, **index_handler(path)(command)}
    (path / "out" / f"{command['id']}.json").write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    text = asyncio.run(server.result(command["id"], wait=1))
    assert "Индекс метаданных построен заново" in text and "Индекс загружен" in text
    with FakeBridge(path) as later:
        found = asyncio.run(server.find("взаиморасчеты"))
    assert later.commands == []
    assert "РегистрНакопления.Взаиморасчеты (Остатки)" in found


def test_index_denied_and_progress_in_sessions(root):
    path = make_session(root, state="busy", busyCommand="x", progress="индекс метаданных: 45%")
    assert "выполняет команду x, индекс метаданных: 45%" in asyncio.run(server.sessions())
    info = json.loads((path / "session.json").read_text(encoding="utf-8"))
    info.update(state="ready", progress="")
    (path / "session.json").write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    with FakeBridge(path, lambda command: {"status": "denied", "error": "не разрешил"}):
        denied = asyncio.run(server.find("инн"))
    assert "Пользователь не разрешил выдачу" in denied
    assert server._indexes == {}
