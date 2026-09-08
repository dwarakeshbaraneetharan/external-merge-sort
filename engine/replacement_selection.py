"""Engine A: replacement selection run generation (Knuth, TAOCP Vol. 3, 5.4.1).

Keeps a min-heap of M records permanently full. Emit the smallest record, read
a replacement: if it is >= the record just emitted it still belongs to the
current run, otherwise it is parked for the next run and the heap shrinks by
one. On random input this averages runs of 2M instead of M.

Everything happens inside one pre-allocated list:

    buf: [0 .......... heap_size) [holes) [filled-parked ...... filled)
          active min-heap                  records held for the next run

Invariant: heap_size + parked == filled until the input runs out. Parked
records grow leftwards from the end, so they can never collide with the heap --
when they would meet, heap_size is 0 and the run ends.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .io_channel import BinaryRunReader, BinaryRunWriter, IOConfig, IOStats


def sift_down(buf: list[int], start: int, size: int) -> int:
    """Push buf[start] down until the heap property holds. Returns levels moved.

    `item` is held in a local and the children are compared against each other
    before being compared against it. Comparing them against buf[start] instead
    reads a slot that has already been overwritten, which breaks the heap
    quietly rather than loudly.
    """
    item = buf[start]
    i = start
    child = 2 * i + 1
    steps = 0
    while child < size:
        right = child + 1
        if right < size and buf[right] < buf[child]:
            child = right
        if buf[child] >= item:
            break
        buf[i] = buf[child]
        i = child
        child = 2 * i + 1
        steps += 1
    buf[i] = item
    return steps


def heapify(buf: list[int], size: int) -> int:
    steps = 0
    for i in reversed(range(size // 2)):
        steps += sift_down(buf, i, size)
    return steps


@dataclass
class RunGenerationResult:
    run_paths: list[str]
    run_lengths: list[int]
    sift_steps: int

    @property
    def run_count(self) -> int:
        return len(self.run_paths)


class ReplacementSelectionEngine:
    name = "knuth"

    def __init__(self, run_dir, capacity_records, io_config=None, stats=None):
        self.run_dir = run_dir
        self.capacity = max(4, capacity_records)
        self.io_config = io_config or IOConfig()
        self.stats = stats or IOStats()
        os.makedirs(run_dir, exist_ok=True)

    def _run_path(self, index: int) -> str:
        return os.path.join(self.run_dir, f"run_knuth_{index:05d}.bin")

    def generate_runs(self, input_path: str) -> RunGenerationResult:
        reader = BinaryRunReader(input_path, self.io_config, self.stats)
        run_paths: list[str] = []
        run_lengths: list[int] = []
        sift_steps = 0

        buf = [0] * self.capacity  # the only allocation the algorithm makes
        filled = 0
        while filled < self.capacity:
            value = reader.read_record()
            if value is None:
                break
            buf[filled] = value
            filled += 1

        if filled == 0:
            reader.close()
            return RunGenerationResult([], [], 0)

        heap_size = filled
        parked = 0
        sift_steps += heapify(buf, heap_size)

        run_index = 0
        writer = BinaryRunWriter(self._run_path(0), self.io_config, self.stats)
        run_paths.append(writer.path)
        emitted = 0
        last_emitted = -1  # keys are unsigned, so -1 is a safe "nothing yet"
        input_done = False

        while heap_size > 0:
            smallest = buf[0]
            writer.write_record(smallest)
            emitted += 1
            last_emitted = smallest

            incoming = None if input_done else reader.read_record()

            if incoming is None:
                input_done = True
                # Drain: promote the last active record. The slot it leaves is
                # a hole, but `parked` is tracked separately so the reservoir
                # at the tail is untouched.
                heap_size -= 1
                if heap_size:
                    buf[0] = buf[heap_size]
                    sift_steps += sift_down(buf, 0, heap_size)
            elif incoming >= last_emitted:
                buf[0] = incoming
                sift_steps += sift_down(buf, 0, heap_size)
            else:
                # Too small for this run. Shrinking the heap frees exactly the
                # slot the parked record needs, so deferring costs no memory.
                heap_size -= 1
                boundary = buf[heap_size]
                buf[heap_size] = incoming
                parked += 1
                if heap_size:
                    buf[0] = boundary
                    sift_steps += sift_down(buf, 0, heap_size)
                # If heap_size hit 0, `boundary` is the record already emitted
                # this iteration, so dropping it is correct.

            if heap_size == 0:
                writer.close()
                run_lengths.append(emitted)
                emitted = 0
                if parked == 0:
                    break
                # The parked records become the next run's heap. Slide them to
                # the front (a C-level move) rather than allocating again.
                if parked < filled:
                    buf[0:parked] = buf[filled - parked : filled]
                filled = heap_size = parked
                parked = 0
                sift_steps += heapify(buf, heap_size)
                last_emitted = -1
                run_index += 1
                writer = BinaryRunWriter(self._run_path(run_index), self.io_config, self.stats)
                run_paths.append(writer.path)

        writer.close()
        if emitted:
            run_lengths.append(emitted)
        reader.close()
        return RunGenerationResult(run_paths, run_lengths, sift_steps)
