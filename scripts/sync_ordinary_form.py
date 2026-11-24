"""Переносит общие области модуля управляемой формы в модуль обычной формы и собирает Form.bin.

Тестирование интерфейса и обмен файлами в обеих формах работают одинаково, поэтому их код пишется один раз - в
модуле управляемой формы, в областях из SHARED_REGIONS. Обычная форма получает те же области без директив
компиляции: в толстом клиенте обычного приложения их нет. После правки общей области:
python3 scripts/sync_ordinary_form.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGED = ROOT / "epf" / "MCPLocalBridge" / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl"
ORDINARY = ROOT / "epf" / "ordinary-form" / "Module.bsl"
SHARED_REGIONS = ("ТестированиеИнтерфейса", "ОбменФайлами")
BOM = b"\xef\xbb\xbf"


def read_module(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def region_span(module: str, name: str) -> tuple[int, int]:
    """Границы области верхнего уровня вместе с её #Область и #КонецОбласти."""
    start = re.search(rf"^#Область {name}\r?$", module, re.M)
    if start is None:
        raise ValueError(f"В модуле нет области {name}")
    depth = 0
    for mark in re.finditer(r"^#(Область|КонецОбласти)\b[^\r\n]*", module[start.start():], re.M):
        depth += 1 if mark.group(1) == "Область" else -1
        if depth == 0:
            return start.start(), start.start() + mark.end()
    raise ValueError(f"Область {name} не закрыта")


def shared_region(managed: str, name: str) -> str:
    """Текст области для обычной формы: как в управляемой, но без директив компиляции."""
    begin, end = region_span(managed, name)
    return re.sub(r"^&НаКлиенте\r?\n", "", managed[begin:end], flags=re.M)


def synced(ordinary: str, managed: str) -> str:
    for name in SHARED_REGIONS:
        begin, end = region_span(ordinary, name)
        ordinary = ordinary[:begin] + shared_region(managed, name) + ordinary[end:]
    return ordinary


def main() -> int:
    ordinary = synced(read_module(ORDINARY), read_module(MANAGED))
    ORDINARY.write_bytes(BOM + ordinary.encode("utf-8"))
    print(f"OK: {ORDINARY.relative_to(ROOT)}")
    spec = importlib.util.spec_from_file_location("pack_ordinary_form", ROOT / "scripts" / "pack_ordinary_form.py")
    pack_ordinary_form = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack_ordinary_form)
    return pack_ordinary_form.main()


if __name__ == "__main__":
    sys.exit(main())
