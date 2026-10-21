"""MCP-сервер локального моста: инструменты агента поверх обмена с обработкой MCPLocalBridge."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from . import exchange
from .exchange import BridgeError
from .paths import exchange_root
from .render import fit, render_response

INSTRUCTIONS = """\
Локальный мост к открытому сеансу 1С пользователя: команды выполняет внешняя обработка MCPLocalBridge.epf,
открытая в клиенте 1С, обмен идёт через каталог на этом компьютере, без внешних серверов.
Начинай с sessions: там видно, к какой базе и под каким пользователем открыт мост и что разрешено на форме.
Каждая команда выполняется в живом сеансе пользователя и занимает его окно 1С: ограничивай выборки и не запускай
долгие переборы без нужды. Серверный код eval/execute по умолчанию выполняется в транзакции с откатом.
Сохранить изменения можно только с allow_write=true и при включённом на форме разрешении «Запись в базу»;
пока оно выключено (так при открытии формы), запрос записи тоже выполняется с откатом и с предупреждением.
Если на форме выбрана выдача результатов с подтверждением, пользователь смотрит каждый результат и решает,
отдавать ли его: давай таким командам timeout побольше, а ответ без решения забирай инструментом result."""

mcp = FastMCP("1c-local-bridge", instructions=INSTRUCTIONS)

SessionArg = Annotated[str | None, Field(
    description="Сеанс моста: идентификатор из sessions или часть строки соединения, имени конфигурации, "
                "пользователя. Не указан - единственный открытый сеанс.")]
TimeoutArg = Annotated[int, Field(
    ge=1, le=280,
    description="Сколько секунд ждать ответ. Команда, которую обработка не начала за это время, снимается; "
                "начатая выполняется дальше, её результат забирается инструментом result.")]
FormatArg = Annotated[Literal["table", "json"], Field(
    description="table - таблицы и сводки для чтения, ссылки показаны представлениями; json - ответ как есть, "
                "с типами и UUID ссылок.")]
SideArg = Annotated[Literal["server", "client"], Field(
    description="server - на сервере 1С; client - в модуле формы обработки на клиенте (нужно разрешение "
                "«Код на клиенте» на форме, транзакции и отката нет).")]
AllowWriteArg = Annotated[bool, Field(
    description="true - выполнить без отката транзакции, изменения сохранятся, если на форме включено разрешение "
                "«Запись в базу». Пока оно выключено, код всё равно выполнится с откатом, в ответе будет "
                "предупреждение.")]

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_kinds: dict[str, str] = {}


@mcp.tool()
async def sessions() -> str:
    """Сеансы 1С, в которых открыта форма обработки моста: база, пользователь, конфигурация, состояние и
    разрешения. Обработку (dist/MCPLocalBridge.epf) пользователь открывает в нужной базе через Файл - Открыть."""
    root = exchange_root()
    return exchange.describe_sessions(exchange.list_sessions(root), root)


@mcp.tool()
async def ping(session: SessionArg = None, timeout: TimeoutArg = 20) -> str:
    """Проверка связи: обработка забирает команду и делает вызов сервера 1С (время сервера, номер сеанса)."""
    return await _run("ping", {}, session, timeout, "json")


@mcp.tool()
async def query(
    text: Annotated[str, Field(description="Текст запроса 1С; пакет из нескольких запросов допустим.")],
    params: Annotated[dict[str, Any] | None, Field(
        description="Параметры запроса: {\"Имя\": значение}. Подробности формата - в описании инструмента.")] = None,
    session: SessionArg = None,
    row_limit: Annotated[int, Field(ge=1, le=100000, description="Сколько строк каждого результата вернуть.")] = 1000,
    format: FormatArg = "table",
    timeout: TimeoutArg = 60,
) -> str:
    """Выполняет запрос 1С на сервере с правами пользователя сеанса и возвращает результаты всех запросов пакета.

    Значения параметров: строка, число, булево и null передаются как есть, массив - список значений
    (для В (&Список)). Особые значения - объекты:
    {"_date": "2026-10-21T00:00:00"} - дата (можно "2026-10-21");
    {"_type": "Справочник.Контрагенты", "_uuid": "..."} - ссылка; ссылку из результата format=json
    можно передать целиком;
    {"_type": "Перечисление.ВидыКонтрагентов", "_name": "ЮридическоеЛицо"} - значение перечисления;
    {"_predefined": "Справочник.Валюты.Рубль"} - предопределённое значение;
    {"_expr": "НачалоГода(ТекущаяДатаСеанса())"} - выражение 1С, вычисленное на сервере.
    РАЗРЕШЕННЫЕ и ПЕРВЫЕ в текст не добавляются: пишите их сами."""
    fields: dict[str, Any] = {"text": text, "rowLimit": row_limit}
    if params:
        fields["params"] = _normalize_params(params)
    return await _run("query", fields, session, timeout, format)


@mcp.tool(name="eval")
async def eval_expression(
    expression: Annotated[str, Field(description="Выражение 1С для Вычислить().")],
    session: SessionArg = None,
    side: SideArg = "server",
    allow_write: AllowWriteArg = False,
    format: FormatArg = "json",
    timeout: TimeoutArg = 60,
) -> str:
    """Вычисляет выражение 1С (Вычислить) и возвращает значение. Ссылки приходят типом, UUID и представлением,
    объекты - реквизитами и табличными частями, таблицы и выборки - строками (не больше 1000).
    На сервере выражение вычисляется в транзакции, которая затем откатывается (если не allow_write)."""
    fields = {"expression": expression, "side": side, "allowWrite": allow_write}
    return await _run("eval", fields, session, timeout, format)


@mcp.tool()
async def execute(
    code: Annotated[str, Field(description="Код 1С: операторы без объявлений Процедура/Функция; результат "
                                           "присвойте переменной Результат.")],
    session: SessionArg = None,
    side: SideArg = "server",
    allow_write: AllowWriteArg = False,
    format: FormatArg = "json",
    timeout: TimeoutArg = 60,
) -> str:
    """Выполняет фрагмент кода 1С (Выполнить) и возвращает значение переменной Результат.

    На сервере код по умолчанию идёт в транзакции, которая после выполнения откатывается: изменения в базе
    не сохраняются. Внешние эффекты откат не отменяет: файлы, HTTP, почта, запуск фоновых заданий, записи
    журнала регистрации. Сообщения пользователю (Сообщить) с сервера возвращаются в ответе.
    Выполнение занимает окно 1С пользователя: пока код работает, интерфейс его сеанса не отвечает."""
    fields = {"code": code, "side": side, "allowWrite": allow_write}
    return await _run("execute", fields, session, timeout, format)


@mcp.tool()
async def metadata(
    path: Annotated[str, Field(
        description="Полное имя объекта: \"Справочник.Контрагенты\", \"РегистрСведений.КурсыВалют\", "
                    "\"Документ.Заказ.ТабличнаяЧасть.Товары\". Пусто - состав конфигурации.")] = "",
    filter: Annotated[str, Field(description="Для состава конфигурации: подстрока имени или синонима.")] = "",
    session: SessionArg = None,
    format: FormatArg = "table",
    timeout: TimeoutArg = 60,
) -> str:
    """Метаданные конфигурации сеанса, включая объекты расширений: без path - состав по коллекциям
    (с filter - только подходящие объекты), с path - реквизиты, табличные части, измерения, ресурсы,
    значения перечисления, движения, формы, команды и макеты объекта."""
    return await _run("metadata", {"path": path, "filter": filter}, session, timeout, format)


@mcp.tool()
async def result(
    command_id: Annotated[str, Field(description="Идентификатор команды из ответа, не уложившегося в timeout.")],
    wait: Annotated[int, Field(ge=0, le=280, description="Сколько секунд подождать, если команда ещё идёт.")] = 30,
    format: Annotated[Literal["table", "json"] | None, Field(
        description="Как показать результат; по умолчанию - как у исходной команды.")] = None,
) -> str:
    """Результат команды, которая выполнялась дольше своего timeout."""
    kind = _kinds.get(command_id, "")
    output_format = format or ("table" if kind in ("query", "metadata") else "json")
    cached = exchange.cached_result(command_id)
    if cached is not None:
        return fit(render_response(cached, kind, output_format), cached, command_id)
    session_path = exchange.locate_command(exchange_root(), command_id)
    if session_path is None:
        raise ToolError(f"Команда {command_id} не найдена: ответ уже забран и забыт или сеанс закрыт.")
    status, response = await exchange.wait_result(session_path, command_id, wait)
    if status == "done" and response is not None:
        return fit(render_response(response, kind, output_format), response, command_id)
    state = exchange.command_state(session_path, command_id)
    if state == "closed":
        raise ToolError("Форма обработки закрыта: каталог сеанса удалён, результата не будет.")
    if state == "queued":
        return f"Команда {command_id} ещё в очереди: обработка занята другой командой или приостановлена."
    if exchange.awaiting_approval(exchange_root(), session_path, command_id):
        return f"Результат команды {command_id} ещё ждёт решения пользователя в 1С."
    return f"Команда {command_id} ещё выполняется."


@mcp.tool()
async def cancel(command_id: Annotated[str, Field(description="Идентификатор команды.")]) -> str:
    """Снимает команду, которую обработка ещё не начала выполнять."""
    root = exchange_root()
    session_path = exchange.locate_command(root, command_id)
    if session_path is None:
        return f"Команда {command_id} не найдена."
    if exchange.withdraw(session_path, command_id):
        return f"Команда {command_id} снята и выполняться не будет."
    state = exchange.command_state(session_path, command_id)
    if state == "running":
        return f"Команда {command_id} уже выполняется, снять её нельзя."
    return f"Команда {command_id} уже выполнена, результат - инструментом result."


async def _run(kind: str, fields: dict[str, Any], selector: str | None, timeout: int, output_format: str) -> str:
    root = exchange_root()
    try:
        target = exchange.find_session(root, selector)
        command_id = exchange.submit(target, kind, fields, ttl_sec=timeout)
    except BridgeError as error:
        raise ToolError(str(error)) from error
    _kinds[command_id] = kind
    status, response = await exchange.wait_result(target.path, command_id, timeout)
    if status == "timeout":
        if exchange.withdraw(target.path, command_id):
            current = next((s for s in exchange.list_sessions(root, prune=False) if s.sid == target.sid), None)
            state = exchange.state_text(current) if current else "каталог сеанса удалён"
            raise ToolError(f"Обработка не взяла команду за {timeout} с, команда снята и выполняться не будет. "
                            f"Сеанс {target.sid}: {state}.")
        response = exchange.take_result(target.path, command_id)
        if response is None:
            if exchange.awaiting_approval(root, target.path, command_id):
                return (f"Команда {command_id} выполнена, результат ждёт решения пользователя в 1С "
                        f"(выдача с подтверждением). Ответ - инструментом result(command_id=\"{command_id}\").")
            return (f"Команда {command_id} выполняется дольше {timeout} с. "
                    f"Результат - инструментом result(command_id=\"{command_id}\").")
    elif status == "closed":
        raise ToolError(f"Форма обработки закрыта во время ожидания (каталог сеанса {target.sid} удалён). "
                        f"Команда {command_id} могла выполниться без ответа - проверьте результат отдельно.")
    assert response is not None
    return fit(render_response(response, kind, output_format), response, command_id)


def _normalize_params(value: Any) -> Any:
    if isinstance(value, dict):
        if isinstance(value.get("_date"), str):
            return {**value, "_date": _normalize_date(value["_date"])}
        return {key: _normalize_params(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_params(item) for item in value]
    return value


def _normalize_date(text: str) -> str:
    text = text.strip().rstrip("Zz").replace(" ", "T")
    if _DATE_ONLY.match(text):
        return text + "T00:00:00"
    if len(text) == 16:  # 2026-10-21T12:30
        return text + ":00"
    return text[:19]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
