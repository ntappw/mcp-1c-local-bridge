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
