"""Engine B: load-sort-store run generation, i.e. what modern databases do.

Fill a buffer with M records, sort it, write it out, repeat. Every run is
exactly M long, so this produces about twice as many runs as replacement
selection on random data -- but the sort is Timsort in C over a contiguous
block, with none of the per-record heap maintenance.
"""

from __future__ import annotations

import os

from .io_channel import BinaryRunReader, BinaryRunWriter, IOConfig, IOStats
from .replacement_selection import RunGenerationResult


class ChunkedSortEngine:
    name = "chunked"

    def __init__(self, run_dir, capacity_records, io_config=None, stats=None):
        self.run_dir = run_dir
        self.capacity = max(4, capacity_records)
        self.io_config = io_config or IOConfig()
        self.stats = stats or IOStats()
        os.makedirs(run_dir, exist_ok=True)

    def generate_runs(self, input_path: str) -> RunGenerationResult:
        reader = BinaryRunReader(input_path, self.io_config, self.stats)
        run_paths: list[str] = []
        run_lengths: list[int] = []
        chunk: list[int] = []  # reused every pass; clear() keeps the capacity

        while True:
            chunk.clear()
            count = reader.read_into(chunk, self.capacity)
            if count == 0:
                break
            chunk.sort()

            path = os.path.join(self.run_dir, f"run_chunked_{len(run_paths):05d}.bin")
            with BinaryRunWriter(path, self.io_config, self.stats) as writer:
                writer.write_records(chunk)
            run_paths.append(path)
            run_lengths.append(count)

        reader.close()
        return RunGenerationResult(run_paths, run_lengths, sift_steps=0)
