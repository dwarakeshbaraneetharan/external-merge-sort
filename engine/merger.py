"""Cascading k-way merge.

Merging every run at once fails once the run count passes the open file limit
(1024 on Linux, 512 for the Windows CRT), and each open run also needs a read
buffer, so fan-in trades against the memory budget too. This merges at most
`max_fan_in` files per batch and repeats until one file is left.

Passes = ceil(log_fanin(runs)), which is the only channel through which having
fewer runs can actually pay off.
"""

from __future__ import annotations

import heapq
import math
import os
from dataclasses import dataclass

from .io_channel import BinaryRunReader, BinaryRunWriter, IOConfig, IOStats


@dataclass
class MergeResult:
    passes: int = 0
    max_open_files: int = 0
    records_written: int = 0


class CascadingKWayMerger:
    def __init__(self, max_fan_in: int = 32, io_config=None, stats=None):
        if max_fan_in < 2:
            raise ValueError("max_fan_in must be at least 2")
        self.max_fan_in = max_fan_in
        self.io_config = io_config or IOConfig()
        self.stats = stats or IOStats()

    def expected_passes(self, run_count: int) -> int:
        return 0 if run_count <= 1 else math.ceil(math.log(run_count, self.max_fan_in))

    def _merge_batch(self, inputs: list[str], output: str) -> int:
        """Merge sorted files into one. Memory is O(fan-in), not O(data)."""
        readers = [BinaryRunReader(path, self.io_config, self.stats) for path in inputs]
        heap = []
        for index, reader in enumerate(readers):
            value = reader.read_record()
            if value is not None:
                heap.append((value, index))
        heapq.heapify(heap)

        written = 0
        with BinaryRunWriter(output, self.io_config, self.stats) as writer:
            # Locals: this loop runs once per record per pass.
            heapreplace, heappop = heapq.heapreplace, heapq.heappop
            write_record = writer.write_record
            while heap:
                value, index = heap[0]
                write_record(value)
                written += 1
                nxt = readers[index].read_record()
                if nxt is None:
                    heappop(heap)
                else:
                    heapreplace(heap, (nxt, index))  # one sift, not two

        for reader in readers:
            reader.close()
        for path in inputs:
            if path != output and os.path.exists(path):
                os.remove(path)
        return written

    def merge(self, run_paths: list[str], output_path: str) -> MergeResult:
        result = MergeResult()

        if not run_paths:
            open(output_path, "wb").close()
            return result

        if len(run_paths) == 1:
            # Replacement selection on presorted input ends up here: the single
            # run is already the answer, so there is no merge phase at all.
            if os.path.exists(output_path):
                os.remove(output_path)
            os.replace(run_paths[0], output_path)
            result.max_open_files = 1
            result.records_written = os.path.getsize(output_path) // 8
            return result

        scratch = os.path.dirname(output_path) or "."
        current = list(run_paths)
        pass_index = 0

        while len(current) > self.max_fan_in:
            next_generation = []
            for batch_index, start in enumerate(range(0, len(current), self.max_fan_in)):
                batch = current[start : start + self.max_fan_in]
                if len(batch) == 1:
                    next_generation.append(batch[0])  # carry it forward, don't copy
                    continue
                intermediate = os.path.join(scratch, f"cascade_p{pass_index}_b{batch_index}.bin")
                self._merge_batch(batch, intermediate)
                result.max_open_files = max(result.max_open_files, len(batch))
                next_generation.append(intermediate)
            current = next_generation
            pass_index += 1

        result.records_written = self._merge_batch(current, output_path)
        result.max_open_files = max(result.max_open_files, len(current))
        result.passes = pass_index + 1
        return result
