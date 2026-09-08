"""Buffered binary I/O for fixed-width 64-bit records."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

RECORD_SIZE = 8
DEFAULT_BLOCK_RECORDS = 8192  # 64 KiB blocks


@dataclass
class IOConfig:
    """Storage settings. latency_ms fakes a slower device (see README)."""

    block_records: int = DEFAULT_BLOCK_RECORDS
    latency_ms: float = 0.0

    @property
    def block_bytes(self) -> int:
        return self.block_records * RECORD_SIZE


@dataclass
class IOStats:
    """Shared counters so both engines are measured the same way."""

    bytes_read: int = 0
    bytes_written: int = 0
    read_ops: int = 0
    write_ops: int = 0

    @property
    def total_ops(self) -> int:
        return self.read_ops + self.write_ops


def _pause(config: IOConfig) -> None:
    if config.latency_ms:
        time.sleep(config.latency_ms / 1000.0)


class BinaryRunWriter:
    """Collects records and writes them out one block at a time."""

    def __init__(self, path: str, config: IOConfig | None = None, stats: IOStats | None = None):
        self.path = path
        self.config = config or IOConfig()
        self.stats = stats or IOStats()
        # buffering=0 so one write here is one syscall, which keeps the
        # IOStats counters meaningful.
        self._file = open(path, "wb", buffering=0)
        self._buffer: list[int] = []
        self.records_written = 0

    def write_record(self, value: int) -> None:
        self._buffer.append(value)
        if len(self._buffer) >= self.config.block_records:
            self._flush()

    def write_records(self, values: Iterable[int]) -> None:
        for value in values:
            self.write_record(value)

    def _flush(self) -> None:
        if not self._buffer:
            return
        # Packing block by block instead of the whole chunk at once: a single
        # struct.pack over a few million values builds an argument tuple that
        # size, which breaks the memory budget.
        payload = struct.pack(f"<{len(self._buffer)}Q", *self._buffer)
        _pause(self.config)
        self._file.write(payload)
        self.stats.bytes_written += len(payload)
        self.stats.write_ops += 1
        self.records_written += len(self._buffer)
        self._buffer.clear()

    def close(self) -> None:
        if not self._file.closed:
            self._flush()
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class BinaryRunReader:
    """Reads a block at a time and hands back records one by one."""

    def __init__(self, path: str, config: IOConfig | None = None, stats: IOStats | None = None):
        self.path = path
        self.config = config or IOConfig()
        self.stats = stats or IOStats()
        self._file = open(path, "rb", buffering=0)
        self._block: tuple[int, ...] = ()
        self._pos = 0
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        _pause(self.config)
        raw = self._file.read(self.config.block_bytes)
        count = len(raw) // RECORD_SIZE
        if count == 0:
            self._eof = True
            return False
        self.stats.bytes_read += len(raw)
        self.stats.read_ops += 1
        # One unpack per block is much faster than one per record.
        self._block = struct.unpack(f"<{count}Q", raw[: count * RECORD_SIZE])
        self._pos = 0
        return True

    def read_record(self) -> int | None:
        if self._pos >= len(self._block) and not self._fill():
            return None
        value = self._block[self._pos]
        self._pos += 1
        return value

    def read_into(self, out: list[int], limit: int) -> int:
        """Append up to `limit` records to `out`, returning how many."""
        added = 0
        while added < limit:
            if self._pos >= len(self._block) and not self._fill():
                break
            take = min(limit - added, len(self._block) - self._pos)
            out.extend(self._block[self._pos : self._pos + take])
            self._pos += take
            added += take
        return added

    def stream_all(self) -> Iterator[int]:
        while True:
            if self._pos >= len(self._block) and not self._fill():
                return
            block, start = self._block, self._pos
            self._pos = len(block)
            yield from block[start:]

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
