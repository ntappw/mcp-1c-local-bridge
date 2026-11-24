"""Обычная форма обработки: Form.bin в репозитории собран из текущих исходников epf/ordinary-form."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pack_ordinary_form", ROOT / "scripts" / "pack_ordinary_form.py")
pack_ordinary_form = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack_ordinary_form)


def test_form_bin_matches_sources():
    # Модуль обычной формы правится в epf/ordinary-form; после правки - python3 scripts/pack_ordinary_form.py
    assert pack_ordinary_form.TARGET.read_bytes() == pack_ordinary_form.build()


def test_container_holds_layout_and_module():
    streams = pack_ordinary_form.unpack(pack_ordinary_form.TARGET.read_bytes())
    assert set(streams) == {"form", "module"}
    module = streams["module"].decode("utf-8-sig")
    assert "Процедура ПередОткрытием(Отказ, СтандартнаяОбработка)" in module
    layout = streams["form"].decode("utf-8-sig")
    # Основной реквизит формы - объект этой обработки (идентификатор типа из MCPLocalBridge.xml), иначе в модуле
    # нет ни ЭтотОбъект, ни свойств объекта; обработчик ПередОткрытием назначен в макете
    xml = (ROOT / "epf" / "MCPLocalBridge.xml").read_text(encoding="utf-8-sig")
    type_id = re.search(r'ExternalDataProcessorObject\.MCPLocalBridge" category="Object">\s*<xr:TypeId>([0-9a-f-]+)<',
                        xml)
    assert '"ОбработкаОбъект"' in layout and type_id.group(1) in layout
    assert '{3,"ПередОткрытием"' in layout


def test_handlers_assigned_in_code_are_export_procedures():
    # Кроме ПередОткрытием, обработчики назначаются кодом и должны быть экспортными процедурами модуля
    # (так требует УстановитьДействие); опечатка в имени видна только при открытии формы в толстом клиенте
    module = (pack_ordinary_form.SOURCES / "Module.bsl").read_text(encoding="utf-8-sig")
    names = set(re.findall(r'Новый Действие\("(\w+)"\)', module))
    names |= set(re.findall(r'ПодключитьОбработчикОжидания\("(\w+)"', module))
    assert names
    for name in names:
        assert re.search(rf"^Процедура {name}\([^)]*\) Экспорт", module, re.M), name


def test_bridge_version_matches_package():
    # Версию обработки пишут в session.json обе формы, она должна совпадать с версией пакета
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    managed = ROOT / "epf" / "MCPLocalBridge" / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl"
    for module in (managed, pack_ordinary_form.SOURCES / "Module.bsl"):
        assert f'"bridgeVersion", "{version}"' in module.read_text(encoding="utf-8-sig"), module


def test_shared_regions_match_managed_form():
    # Тестирование интерфейса и обмен файлами пишутся в модуле управляемой формы; в обычную их переносит
    # python3 scripts/sync_ordinary_form.py
    spec = importlib.util.spec_from_file_location("sync_ordinary_form", ROOT / "scripts" / "sync_ordinary_form.py")
    sync_ordinary_form = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync_ordinary_form)
    managed = sync_ordinary_form.read_module(sync_ordinary_form.MANAGED)
    ordinary = sync_ordinary_form.read_module(sync_ordinary_form.ORDINARY)
    assert sync_ordinary_form.synced(ordinary, managed) == ordinary
    for name in sync_ordinary_form.SHARED_REGIONS:
        assert "&На" not in sync_ordinary_form.shared_region(managed, name)


def test_modules_keep_bom_crlf_and_indented_blank_lines():
    # Модули - UTF-8 с BOM и CRLF; пустая строка внутри метода несёт отступ, на уровне модуля - пустая
    managed = ROOT / "epf" / "MCPLocalBridge" / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl"
    module_object = ROOT / "epf" / "MCPLocalBridge" / "Ext" / "ObjectModule.bsl"
    for path in (managed, pack_ordinary_form.SOURCES / "Module.bsl", module_object):
        data = path.read_bytes()
        assert data.startswith(b"\xef\xbb\xbf"), path
        text = data.decode("utf-8-sig")
        assert not re.search(r"(?<!\r)\n", text), path
        inside = False
        for number, line in enumerate(text.split("\r\n"), 1):
            if re.match(r"\s*(Функция|Процедура)\s", line):
                inside = True
            elif re.match(r"\s*(КонецФункции|КонецПроцедуры)", line):
                inside = False
            elif inside:
                assert line != "", f"{path.name}:{number}"
            assert len(line.expandtabs(4)) <= 120, f"{path.name}:{number}"


def test_ordinary_form_methods_do_not_shadow_object_exports():
    # В модуле обычной формы экспортные методы модуля объекта видны без ЭтотОбъект: метод формы с тем же именем
    # не компилируется («Недостаточно фактических параметров»), а ошибку видно только при открытии формы
    object_module = (ROOT / "epf" / "MCPLocalBridge" / "Ext" / "ObjectModule.bsl").read_text(encoding="utf-8-sig")
    exports = set(re.findall(r"^(?:Функция|Процедура) (\w+)\([^)]*\) Экспорт", object_module, re.M))
    module = (pack_ordinary_form.SOURCES / "Module.bsl").read_text(encoding="utf-8-sig")
    own = set(re.findall(r"^(?:Функция|Процедура) (\w+)\(", module, re.M))
    assert exports and not exports & own
