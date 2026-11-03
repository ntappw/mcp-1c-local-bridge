"""Собирает Form.bin обычной формы обработки из текстовых исходников epf/ordinary-form.

Обычная форма в XML-выгрузке - двоичный контейнер 1С с двумя текстовыми потоками: form (макет во
внутреннем формате) и module (модуль). Править удобнее текст, поэтому исходники лежат отдельно, а
контейнер собирается этим скриптом: python3 scripts/pack_ordinary_form.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "epf" / "ordinary-form"
TARGET = ROOT / "epf" / "MCPLocalBridge" / "Forms" / "ФормаОбычная" / "Ext" / "Form.bin"
PAGE = 512
NO_BLOCK = 0x7FFFFFFF
# Время создания и изменения элементов: постоянное, чтобы сборка из тех же исходников давала те же байты
STAMP = bytes.fromhex("103da57d96420200")


def block(data: bytes, size: int | None = None) -> bytes:
    """Блок контейнера: заголовок с длиной данных, размером блока и адресом следующего блока, затем данные."""
    size = max(len(data), size or 0)
    header = b"\r\n%08x %08x %08x \r\n" % (len(data), size, NO_BLOCK)
    return header + data + b"\x00" * (size - len(data))


def pack(streams: list[tuple[str, bytes]]) -> bytes:
    toc_size = 12 * len(streams)
    offset = 16 + len(block(b"\x00" * toc_size, PAGE))
    toc = b""
    body = b""
    for name, data in streams:
        header = STAMP + STAMP + b"\x00" * 4 + name.encode("utf-16-le") + b"\x00" * 4
        header_block = block(header)
        data_block = block(data, PAGE)
        toc += struct.pack("<III", offset, offset + len(header_block), NO_BLOCK)
        body += header_block + data_block
        offset += len(header_block) + len(data_block)
    file_header = struct.pack("<IIII", NO_BLOCK, PAGE, 2, 0)
    return file_header + block(toc, PAGE) + body


def unpack(data: bytes) -> dict[str, bytes]:
    """Потоки контейнера по именам: для проверки сборки."""

    def read_block(position: int) -> bytes:
        header = data[position:position + 31]
        length, size = int(header[2:10], 16), int(header[11:19], 16)
        return data[position + 31:position + 31 + size][:length]

    toc = read_block(16)
    streams = {}
    for index in range(0, len(toc), 12):
        header_address, data_address, _ = struct.unpack("<III", toc[index:index + 12])
        name = read_block(header_address)[20:].decode("utf-16-le").rstrip("\x00")
        streams[name] = read_block(data_address)
    return streams


def build() -> bytes:
    return pack([("form", (SOURCES / "form.txt").read_bytes()), ("module", (SOURCES / "Module.bsl").read_bytes())])


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(build())
    print(f"OK: {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
