"""Read flat Parquet files with the standard library, for loading public datasets.

Supports what common writers produce for flat tables: required and optional
columns, PLAIN and dictionary encodings, data pages v1 and v2, and no
compression, gzip, or zstd. Nested columns are refused. Values come back as
Python values; timestamps as ISO 8601 strings in UTC.
"""

from compression import zstd
from datetime import datetime, timedelta, timezone
import gzip
import struct
from typing import Any, Iterator

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ParquetError(ValueError):
    pass


class Thrift:
    """Thrift's compact protocol, decoded into dicts keyed by field number."""

    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data, self.position = data, position

    def byte(self) -> int:
        value = self.data[self.position]
        self.position += 1
        return value

    def varint(self) -> int:
        shift = result = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def zigzag(self) -> int:
        value = self.varint()
        return (value >> 1) ^ -(value & 1)

    def value(self, kind: int) -> Any:
        if kind in (1, 2):
            return kind == 1
        if kind == 3:
            return struct.unpack("b", bytes([self.byte()]))[0]
        if kind in (4, 5, 6):
            return self.zigzag()
        if kind == 7:
            self.position += 8
            return struct.unpack_from("<d", self.data, self.position - 8)[0]
        if kind == 8:
            size = self.varint()
            self.position += size
            return self.data[self.position - size:self.position]
        if kind in (9, 10):
            header = self.byte()
            size, item = header >> 4, header & 0x0F
            if size == 15:
                size = self.varint()
            if item in (1, 2):
                return [self.byte() == 1 for _ in range(size)]
            return [self.value(item) for _ in range(size)]
        if kind == 11:
            size = self.varint()
            if not size:
                return {}
            types = self.byte()
            return {self.value(types >> 4): self.value(types & 0x0F) for _ in range(size)}
        if kind == 12:
            return self.struct()
        raise ParquetError(f"Unknown thrift type {kind}.")

    def struct(self) -> dict[int, Any]:
        fields, last = {}, 0
        while True:
            header = self.byte()
            if header == 0:
                return fields
            delta, kind = header >> 4, header & 0x0F
            last = last + delta if delta else self.zigzag()
            fields[last] = self.value(kind)


def _bit_packed(data: bytes, position: int, count: int, width: int) -> list[int]:
    number = int.from_bytes(data[position:position + width * count // 8], "little")
    mask = (1 << width) - 1
    return [(number >> (index * width)) & mask for index in range(count)]


def _hybrid(data: bytes, position: int, end: int, width: int, count: int) -> list[int]:
    """The RLE and bit-packed hybrid encoding of levels and dictionary indexes."""
    values: list[int] = []
    reader = Thrift(data, position)
    while len(values) < count and reader.position < end:
        header = reader.varint()
        if header & 1:
            groups = header >> 1
            values.extend(_bit_packed(data, reader.position, groups * 8, width))
            reader.position += groups * width
        else:
            size = (width + 7) // 8
            value = int.from_bytes(data[reader.position:reader.position + size], "little")
            reader.position += size
            values.extend([value] * (header >> 1))
    return values[:count]


def _plain(data: bytes, position: int, kind: int, count: int, width: int | None) -> list[Any]:
    if kind == 0:
        return [bool(data[position + index // 8] >> (index % 8) & 1) for index in range(count)]
    if kind in (1, 2, 4, 5):
        code = {1: "i", 2: "q", 4: "f", 5: "d"}[kind]
        return list(struct.unpack_from(f"<{count}{code}", data, position))
    if kind == 6:
        values = []
        for _ in range(count):
            (size,) = struct.unpack_from("<I", data, position)
            values.append(data[position + 4:position + 4 + size])
            position += 4 + size
        return values
    if kind == 7:
        return [data[position + index * width:position + (index + 1) * width] for index in range(count)]
    raise ParquetError(f"Physical type {kind} is not supported.")


def _decompress(codec: int, data: bytes) -> bytes:
    if codec == 0:
        return data
    if codec == 2:
        return gzip.decompress(data)
    if codec == 6:
        return zstd.decompress(data)
    raise ParquetError(f"Compression codec {codec} is not supported.")


def _converter(element: dict[int, Any]):
    kind, converted, logical = element.get(1), element.get(6), element.get(10, {})
    if kind == 6 or kind == 7:
        if converted == 0 or 1 in logical or 4 in logical or 6 in logical or converted == 19:  # UTF8, STRING, ENUM, JSON
            return lambda value: value.decode("utf-8", errors="replace")
        return bytes
    if kind == 2 and (converted in (9, 10) or 8 in logical):
        unit = {9: 1_000, 10: 1_000_000}.get(converted, 1_000_000)
        if 8 in logical:
            unit = {1: 1_000, 2: 1_000_000, 3: 1_000_000_000}[next(iter(logical[8][2]))]
        def timestamp(value: int) -> str:
            moment = EPOCH + timedelta(microseconds=value * 1_000_000 // unit)
            return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        return timestamp
    if kind == 1 and (converted == 6 or 5 in logical):  # DATE
        return lambda value: (EPOCH + timedelta(days=value)).date().isoformat()
    return lambda value: value


class ParquetFile:
    def __init__(self, path) -> None:
        with open(path, "rb") as file:
            self.data = file.read()
        if self.data[:4] != b"PAR1" or self.data[-4:] != b"PAR1":
            raise ParquetError(f"{path} is not a Parquet file.")
        (length,) = struct.unpack_from("<I", self.data, len(self.data) - 8)
        meta = Thrift(self.data, len(self.data) - 8 - length).struct()
        root, *elements = meta[2]
        if any(9 in element or element.get(3) == 2 for element in elements):  # children or a repeated field
            raise ParquetError("Nested columns are not supported.")
        self.columns = [element[4].decode() for element in elements]
        self.elements = dict(zip(self.columns, elements))
        self.rows = meta[3]
        self.row_groups = meta.get(4, [])
        self.created_by = meta.get(6, b"").decode()

    def _column(self, chunk: dict[int, Any], element: dict[int, Any]) -> list[Any]:
        meta = chunk[3]
        kind, codec, total = meta[1], meta[4], meta[5]
        optional = element.get(3) == 1
        convert = _converter(element)
        position = meta.get(11) or meta[9]
        dictionary: list[Any] | None = None
        values: list[Any] = []
        while len(values) < total:
            reader = Thrift(self.data, position)
            header = reader.struct()
            start = reader.position
            position = start + header[3]
            page_type, raw = header[1], self.data[start:position]
            if page_type == 2:
                page = _decompress(codec, raw)
                dictionary = [convert(value) for value in _plain(page, 0, kind, header[7][1], element.get(2))]
                continue
            if page_type == 0:
                page, count, encoding = _decompress(codec, raw), header[5][1], header[5][2]
                at, levels = 0, None
                if optional:
                    (size,) = struct.unpack_from("<I", page, 0)
                    levels = _hybrid(page, 4, 4 + size, 1, count)
                    at = 4 + size
            elif page_type == 3:
                v2 = header[8]
                count, encoding = v2[1], v2[4]
                definitions, repetitions = v2.get(5, 0), v2.get(6, 0)
                levels = _hybrid(raw, repetitions, repetitions + definitions, 1, count) if optional else None
                body = raw[repetitions + definitions:]
                page, at = (_decompress(codec, body) if v2.get(7, True) else body), 0
            else:
                continue
            present = count if levels is None else sum(levels)
            if encoding in (2, 8):
                width = page[at]
                found = [dictionary[index] for index in _hybrid(page, at + 1, len(page), width, present)] if width else [dictionary[0]] * present
            elif encoding == 0:
                found = [convert(value) for value in _plain(page, at, kind, present, element.get(2))]
            else:
                raise ParquetError(f"Encoding {encoding} is not supported.")
            if levels is None:
                values.extend(found)
            else:
                items = iter(found)
                values.extend(next(items) if level else None for level in levels)
        return values

    def read(self, columns: list[str] | None = None) -> Iterator[dict[str, Any]]:
        """Yield each row as a dict of the named columns, or of every column."""
        names = columns or self.columns
        missing = set(names) - set(self.columns)
        if missing:
            raise ParquetError(f"No such columns: {', '.join(sorted(missing))}.")
        for group in self.row_groups:
            chunks = {".".join(item.decode() for item in chunk[3][3]): chunk for chunk in group[1]}
            data = {name: self._column(chunks[name], self.elements[name]) for name in names}
            for index in range(group[3]):
                yield {name: data[name][index] for name in names}
