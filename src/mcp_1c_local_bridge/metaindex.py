"""Индекс метаданных сеанса 1С: поиск объектов и полей, обратные связи (формат - docs/protocol.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Составной тип шире этого показывается кратко, а в связях идёт отдельной группой
WIDE_TYPE = 10
FIELD_SECTIONS = (("dimensions", "Измерение"), ("resources", "Ресурс"), ("attributes", "Реквизит"))
NAMED_LISTS = (("forms", "Форма"), ("commands", "Команда"), ("templates", "Макет"))
# Имена коллекций, которыми агент может назвать вид в find, - к виду в полном имени
COLLECTION_KINDS = {
    "константы": "Константа", "справочники": "Справочник", "документы": "Документ",
    "журналыдокументов": "ЖурналДокументов", "перечисления": "Перечисление", "отчеты": "Отчет",
    "обработки": "Обработка", "планывидовхарактеристик": "ПланВидовХарактеристик", "планысчетов": "ПланСчетов",
    "планывидоврасчета": "ПланВидовРасчета", "регистрысведений": "РегистрСведений",
    "регистрынакопления": "РегистрНакопления", "регистрыбухгалтерии": "РегистрБухгалтерии",
    "регистрырасчета": "РегистрРасчета", "бизнеспроцессы": "БизнесПроцесс", "задачи": "Задача",
    "планыобмена": "ПланОбмена", "последовательности": "Последовательность", "критерииотбора": "КритерийОтбора",
    "общиемодули": "ОбщийМодуль", "общиеформы": "ОбщаяФорма", "общиекоманды": "ОбщаяКоманда",
    "общиемакеты": "ОбщийМакет", "общиереквизиты": "ОбщийРеквизит", "определяемыетипы": "ОпределяемыйТип",
    "роли": "Роль", "регламентныезадания": "РегламентноеЗадание", "подпискинасобытия": "ПодпискаНаСобытие",
    "функциональныеопции": "ФункциональнаяОпция", "параметрысеанса": "ПараметрСеанса",
    "хранилищанастроек": "ХранилищеНастроек", "httpсервисы": "HTTPСервис", "webсервисы": "WebСервис",
    "внешниеисточникиданных": "ВнешнийИсточникДанных", "подсистемы": "Подсистема", "реквизиты": "Реквизит",
    "измерения": "Измерение", "ресурсы": "Ресурс", "табличныечасти": "ТабличнаяЧасть", "формы": "Форма",
    "команды": "Команда", "макеты": "Макет", "предопределенные": "Предопределенный",
}
_TYPE_SUFFIX = re.compile(r"^(\w+?)(ссылка|объект|менеджер|наборзаписей|выборка|список)\.(.+)$")


def norm(text: str) -> str:
    return str(text).casefold().replace("ё", "е")


@dataclass
class Entry:
    """Найденная сущность индекса: объект метаданных или его часть."""

    full: str  # полное имя: Справочник.Контрагенты.Реквизит.ИНН
    kind: str  # Справочник, Реквизит, ТабличнаяЧасть, ЗначениеПеречисления...
    name: str
    synonym: str = ""
    owner: str = ""  # полное имя владельца у частей объекта
    owner_synonym: str = ""
    types: tuple[str, ...] = ()
    extension: str = ""
    note: str = ""  # обработчик подписки, метод задания, признаки общего модуля


class MetadataIndex:
    def __init__(self, data: dict[str, Any]) -> None:
        self.fingerprint = str(data.get("fingerprint") or "")
        self.built = str(data.get("built") or "")
        self.configuration = data.get("configuration") or {}
        self.objects: dict[str, dict[str, Any]] = {}
        self.entries: list[Entry] = []
        self.fields: list[Entry] = []
        self.errors: list[str] = []
        for part in data.get("parts") or []:
            types = [tuple(str(name) for name in item) for item in part.get("types") or []]
            for raw in part.get("objects") or []:
                if isinstance(raw, dict) and raw.get("full"):
                    self._add_object(raw, types)
        # Тип поля из определяемого типа в 1С развёрнут в список типов: узнаём его по совпадению состава
        self.defined_types = {frozenset(item["_type"]): item["full"] for item in self.objects.values()
                              if item["_kind"] == "ОпределяемыйТип" and len(item["_type"]) > 1}

    # Построение

    def _add_object(self, raw: dict[str, Any], types: list[tuple[str, ...]]) -> None:
        full = str(raw["full"])
        kind = full.split(".", 1)[0]
        synonym = str(raw.get("synonym") or "")
        item = {**raw, "_kind": kind, "_type": _resolve(types, raw.get("type")),
                "_source": _resolve(types, raw.get("source"))}
        self.objects[norm(full)] = item
        if raw.get("error"):
            self.errors.append(f"{full}: {raw['error']}")
        self.entries.append(Entry(full, kind, str(raw.get("name") or full.rsplit(".", 1)[-1]), synonym,
                                  types=item["_type"], extension=str(raw.get("extension") or ""),
                                  note=_object_note(item)))
        for key, section in FIELD_SECTIONS:
            for field in _items(raw.get(key)):
                self._add_field(full, synonym, section, field, types)
        for table in _items(raw.get("tables")):
            if not isinstance(table, dict):
                continue
            name = str(table.get("name") or "")
            table_synonym = str(table.get("synonym") or "")
            table_full = f"{full}.ТабличнаяЧасть.{name}"
            self.entries.append(Entry(table_full, "ТабличнаяЧасть", name, table_synonym, full, synonym,
                                      extension=str(table.get("extension") or "")))
            for field in _items(table.get("attributes")):
                self._add_field(table_full, f"{synonym} {name} {table_synonym}", "Реквизит", field, types)
        for value in _items(raw.get("values")):
            if isinstance(value, list) and value:
                name = str(value[0])
                self.entries.append(Entry(f"{full}.ЗначениеПеречисления.{name}", "ЗначениеПеречисления", name,
                                          str(value[1]) if len(value) > 1 else "", full, synonym))
        for name in _items(raw.get("predefined")):
            self.entries.append(Entry(f"{full}.{name}", "Предопределенный", str(name), "", full, synonym))
        for key, word in NAMED_LISTS:
            for name in _items(raw.get(key)):
                self.entries.append(Entry(f"{full}.{word}.{name}", word, str(name), "", full, synonym))

    def _add_field(self, owner: str, owner_synonym: str, section: str, field: Any,
                   types: list[tuple[str, ...]]) -> None:
        # Поле - массив: имя, синоним (пусто - как имя), номер типа в таблице типов порции, расширение
        if not isinstance(field, list) or not field:
            return
        name = str(field[0])
        entry = Entry(f"{owner}.{section}.{name}", section, name, str(field[1] or "") if len(field) > 1 else "",
                      owner, owner_synonym, _resolve(types, field[2] if len(field) > 2 else None),
                      str(field[3] or "") if len(field) > 3 else "")
        self.entries.append(entry)
        if entry.types:
            self.fields.append(entry)

    # Поиск

    def find(self, text: str, kind: str = "", limit: int = 50) -> str:
        words = [norm(word) for word in text.split() if word.strip()]
        if not words:
            return "Пустой запрос: передай слова из имени или синонима."
        wanted = norm(kind.strip())
        wanted = norm(COLLECTION_KINDS.get(wanted, wanted))
        found: list[tuple[int, int, str, Entry]] = []
        for entry in self.entries:
            if wanted and wanted not in (norm(entry.kind), norm(entry.full.split(".", 1)[0])):
                continue
            score = _score(entry, words)
            if score:
                found.append((-score, len(entry.full), entry.full, entry))
        if not found:
            return (f"По «{text}» в индексе ничего нет. Ищутся подстроки имён и синонимов, все слова сразу; "
                    "код модулей в индекс не входит.")
        found.sort(key=lambda item: item[:3])
        head = f"Найдено {len(found)}" + (f", показаны первые {limit}" if len(found) > limit else "") + ":"
        return "\n".join([head, *(self.entry_line(item[3]) for item in found[:limit])])

    def usages(self, name: str, limit: int = 200) -> str:
        target = self.canonical(name)
        if target is None:
            return (f"{name}: такого объекта в индексе нет. Нужно полное имя вида Справочник.Контрагенты - "
                    "его можно найти инструментом find.")
        item = self.objects[norm(target)]
        sections: list[tuple[str, list[str]]] = []
        sections.extend(self._type_usages(target))
        objects_with_type = [self.object_line(other) for other in self.objects.values()
                             if target in other["_type"] and other["full"] != target]
        sections.append(("Объекты с этим типом (константы, определяемые типы, параметры сеанса и др.)",
                         objects_with_type))
        sections.append(("Подписки на события с этим источником",
                         [self.object_line(other) for other in self._objects_with(target, "_source")]))
        sections.append(("Документы и последовательности, которые пишут в этот регистр",
                         [other["full"] for other in self._objects_with(target, "records")]))
        sections.append(("Движения этого документа", list(item.get("records") or [])))
        sections.append(("Журналы, последовательности, подсистемы, функциональные опции, общие реквизиты "
                         "и критерии отбора с ним в составе", self._content_usages(target)))
        sections.append(("Планы обмена с ним в составе", [
            f"{other['full']}" + ("" if entry[1] else " (без авторегистрации)")
            for other in self.objects.values() for entry in other.get("exchangeContent") or []
            if isinstance(entry, list) and entry and entry[0] == target]))
        sections.append(("Подчинённые справочники", [other["full"] for other in self._objects_with(target, "owners")]))
        sections.append(("Вводятся на основании этого объекта",
                         [other["full"] for other in self._objects_with(target, "basedOn")]))
        sections.append(("Этот объект вводится на основании", list(item.get("basedOn") or [])))
        sections.append(("Регистры и бизнес-процессы, которые на него опираются", [
            other["full"] for other in self.objects.values()
            if target in (other.get("chart"), other.get("task"), other.get("subconto"))]))
        if item["_kind"] == "ОбщийМодуль":
            module = target.split(".", 1)[1] + "."
            sections.append(("Подписки и регламентные задания, вызывающие методы модуля", [
                self.object_line(other) for other in self.objects.values()
                if str(other.get("handler") or other.get("method") or "").startswith(module)]))
        lines = [f"Связи {self.object_line(item)} по индексу метаданных:"]
        shown = 0
        for title, values in sections:
            if not values:
                continue
            lines.append(f"\n{title} ({len(values)}):")
            room = max(limit - shown, 0)
            lines.extend(f"- {value}" for value in values[:room])
            shown += min(len(values), room)
            if len(values) > room:
                lines.append(f"- ...и ещё {len(values) - room}, увеличь limit")
        if len(lines) == 1:
            lines.append("Связей не найдено.")
        lines.append("\nОбращений из кода модулей в индексе нет: их искать по выгрузке.")
        return "\n".join(lines)

    def _type_usages(self, target: str) -> list[tuple[str, list[str]]]:
        direct: list[str] = []
        through_defined: dict[str, list[str]] = {}
        wide: list[str] = []
        for entry in self.fields:
            if target not in entry.types:
                continue
            defined = self.defined_types.get(frozenset(entry.types)) if len(entry.types) > 1 else None
            if defined:
                through_defined.setdefault(defined, []).append(entry.full)
            elif len(entry.types) > WIDE_TYPE:
                wide.append(f"{entry.full} ({len(entry.types)} типов)")
            else:
                direct.append(self.entry_line(entry))
        sections = [("Поля с этим типом", direct)]
        for defined, fields in sorted(through_defined.items()):
            sections.append((f"Поля типа {defined}, куда входит этот тип", fields))
        sections.append((f"Поля составного типа шире {WIDE_TYPE} типов", wide))
        return sections

    def _content_usages(self, target: str) -> list[str]:
        result = []
        prefix = target + "."
        for other in self.objects.values():
            content = [str(value) for value in other.get("content") or []]
            storage = str(other.get("storage") or "")
            if target in content or any(value.startswith(prefix) for value in content) \
                    or storage == target or storage.startswith(prefix):
                result.append(other["full"])
        return result

    def _objects_with(self, target: str, key: str) -> list[dict[str, Any]]:
        return [other for other in self.objects.values() if target in (other.get(key) or ())]

    def canonical(self, name: str) -> str | None:
        key = norm(name.strip())
        if key in self.objects:
            return self.objects[key]["full"]
        # СправочникСсылка.Контрагенты, ДокументОбъект.Заказ - к полному имени объекта метаданных
        match = _TYPE_SUFFIX.match(key)
        if match and f"{match.group(1)}.{match.group(3)}" in self.objects:
            return self.objects[f"{match.group(1)}.{match.group(3)}"]["full"]
        return None

    # Представление

    def entry_line(self, entry: Entry) -> str:
        line = entry.full
        if entry.synonym and norm(entry.synonym).replace(" ", "") != norm(entry.name):
            line += f" «{entry.synonym}»"
        if entry.types:
            line += ": " + self.types_text(entry.types, entry.full)
        if entry.note:
            line += f" ({entry.note})"
        if entry.extension:
            line += f" [расширение {entry.extension}]"
        return line

    def object_line(self, item: dict[str, Any]) -> str:
        entry = Entry(item["full"], item["_kind"], str(item.get("name") or ""), str(item.get("synonym") or ""),
                      types=item["_type"], extension=str(item.get("extension") or ""), note=_object_note(item))
        return self.entry_line(entry)

    def types_text(self, types: tuple[str, ...], own: str = "") -> str:
        if len(types) > 1:
            defined = self.defined_types.get(frozenset(types))
            if defined and defined != own:
                return f"{defined} ({len(types)} типов)"
        if len(types) <= 4:
            return ", ".join(types)
        return ", ".join(types[:3]) + f" и ещё {len(types) - 3}"


def summary(result: dict[str, Any]) -> str:
    source = "взят из настроек пользователя" if result.get("source") == "saved" else "построен заново"
    built = str(result.get("built") or "").replace("T", " ")[:16]
    return (f"Индекс метаданных {source} (построен {built} UTC): объектов {result.get('objects', '?')}, "
            f"полей {result.get('fields', '?')}. Поиск - find, связи - usages; после загрузки изменений в базу "
            "- refresh=true.")


def _score(entry: Entry, words: list[str]) -> int:
    name = norm(entry.name)
    own = f"{name} {norm(entry.synonym)} {norm(entry.note)}"
    context = f"{norm(entry.owner)} {norm(entry.owner_synonym)}"
    score = 0
    own_matched = False
    for word in words:
        if word == name:
            score += 100
        elif name.startswith(word):
            score += 50
        elif word in name:
            score += 20
        elif word in own:
            score += 10
        elif word in context:
            # Слово о владельце сужает поиск, но одно оно поле не находит
            score += 1
            continue
        else:
            return 0
        own_matched = True
    if not own_matched:
        return 0
    return score + (3 if not entry.owner else 0)


def _items(value: Any) -> list[Any]:
    # Индекс пишет другая сторона: список, где ждём список, иначе - пусто
    return value if isinstance(value, list) else []


def _resolve(types: list[tuple[str, ...]], ref: Any) -> tuple[str, ...]:
    if isinstance(ref, float) and ref.is_integer():
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool) and 0 <= ref < len(types):
        return types[ref]
    return ()


def _object_note(item: dict[str, Any]) -> str:
    if item.get("handler"):
        return f"{item.get('event', '')} -> {item['handler']}"
    if item.get("method"):
        return f"метод {item['method']}" + (", выключено" if item.get("use") is False else "")
    if item.get("flags") is not None:
        flags = [str(flag) for flag in item.get("flags") or []]
        reuse = str(item.get("reuse") or "")
        if reuse and norm(reuse).replace(" ", "") != "неиспользовать":
            flags.append(f"повторное использование: {reuse}")
        return ", ".join(flags)
    if item.get("root"):
        return f"/hs/{item['root']}"
    return str(item.get("registerKind") or item.get("periodicity") or "")
