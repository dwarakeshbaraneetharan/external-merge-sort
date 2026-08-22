"""Runs an engine end to end and records what it cost."""

from __future__ import annotations

import gc
import os
import random
import shutil
import sys
import threading
import time
from dataclasses import asdict, dataclass

import psutil

from engine.chunked_sort import ChunkedSortEngine
from engine.io_channel import RECORD_SIZE, BinaryRunReader, IOConfig, IOStats
from engine.merger import CascadingKWayMerger
from engine.replacement_selection import ReplacementSelectionEngine

ENGINES = {"knuth": ReplacementSelectionEngine, "chunked": ChunkedSortEngine}


# --- resident set size -----------------------------------------------------

_process = psutil.Process()


def current_rss() -> int:
    """Resident set size in bytes.

    Via psutil because `resource` is Unix only and the Windows equivalent is
    thirty lines of ctypes that have nothing to do with sorting.
    """
    return _process.memory_info().rss


class MemoryProbe:
    """Samples RSS on a background thread to get a per-phase peak.

    The OS peak counter is monotonic for the whole process, so it cannot tell
    two engines apart when they run back to back.
    """

    def __init__(self, interval: float = 0.02):
        self.interval = interval
        self.peak = self.baseline = 0
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        gc.collect()
        self.peak = self.baseline = current_rss()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.peak = max(self.peak, current_rss())

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.peak = max(self.peak, current_rss())


def bytes_per_record() -> float:
    """What one buffered record costs in CPython: 8 + sizeof(PyLongObject).

    A key is 8 bytes on disk, but in a list it is an 8-byte pointer plus a heap
    allocated integer object, so sizing the buffer as budget // 8 blows the
    ceiling several times over.

    Derived rather than sampled so the same command gives the same M on every
    run. sys.getsizeof reports 36 bytes for a 63-bit int on CPython 3.12+, and
    the allocator rounds small blocks up to a multiple of 16, giving 56 total.
    measure_bytes_per_record() below checks this against real RSS.
    """
    int_size = sys.getsizeof(1 << 62)
    return 8 + (int_size + 15) // 16 * 16


def measure_bytes_per_record(sample: int = 250_000) -> float:
    """Same figure, measured from RSS instead of derived. Noisy by a few percent."""
    gc.collect()
    before = current_rss()
    rng = random.Random(1)
    buffer = [rng.getrandbits(63) for _ in range(sample)]
    per_record = (current_rss() - before) / sample
    del buffer
    gc.collect()
    return per_record


# --- memory planning -------------------------------------------------------

@dataclass
class MemoryPlan:
    budget_bytes: int
    bytes_per_record: float
    block_records: int
    fan_in: int
    capacity_records: int
    run_phase_bytes: int
    fan_in_clamped: bool = False

    def describe(self) -> str:
        return (
            f"budget={self.budget_bytes / 1e6:.1f} MB  "
            f"bytes/record={self.bytes_per_record:.1f}  "
            f"M={self.capacity_records:,} records  fan-in={self.fan_in}"
        )


def plan_memory(budget_bytes, bytes_per_record, block_records=8192, fan_in=32) -> MemoryPlan:
    """Split the budget between the sort buffer and the I/O buffers.

    A decoded block of 8192 records is 8192 Python ints, not 64 KiB, so during
    the merge the fan-in read buffers are what actually bind -- file descriptor
    limits usually get the blame but memory runs out first.
    """
    per_stream = block_records * bytes_per_record
    affordable = int(budget_bytes * 0.9 / per_stream) - 1
    clamped = affordable < fan_in
    if clamped:
        fan_in = max(2, affordable)

    # Run generation holds the sort buffer plus one input and one output block.
    capacity = max(64, int(budget_bytes / bytes_per_record) - 2 * block_records)
    return MemoryPlan(
        budget_bytes=budget_bytes,
        bytes_per_record=bytes_per_record,
        block_records=block_records,
        fan_in=fan_in,
        capacity_records=capacity,
        run_phase_bytes=int((capacity + 2 * block_records) * bytes_per_record),
        fan_in_clamped=clamped,
    )


# --- verification ----------------------------------------------------------

@dataclass
class FileSummary:
    count: int
    checksum: int
    xor: int
    is_sorted: bool


def summarize_file(path: str) -> FileSummary:
    """One pass: is it sorted, and what is its order-independent fingerprint?

    Comparing (count, sum, xor) before and after catches the case a sortedness
    check misses -- an engine that drops or duplicates records still produces a
    perfectly sorted file.
    """
    mask = (1 << 64) - 1
    count = total = xor = 0
    ordered, previous = True, -1
    with BinaryRunReader(path) as reader:
        for value in reader.stream_all():
            count += 1
            total = (total + value) & mask
            xor ^= value
            if value < previous:
                ordered = False
            previous = value
    return FileSummary(count, total, xor, ordered)


# --- the benchmark ---------------------------------------------------------

@dataclass
class BenchmarkResult:
    engine: str
    distribution: str
    records: int
    budget_mb: float
    capacity_records: int
    fan_in: int
    latency_ms: float

    runs_generated: int = 0
    #: Average run length / M, excluding the final run. The last run is cut
    #: short by end of input rather than by the algorithm, and including it
    #: drags the average well below the 2.0 the theory predicts.
    run_multiplier: float = 0.0
    merge_passes: int = 0
    max_open_files: int = 0

    generation_s: float = 0.0
    merge_s: float = 0.0
    total_s: float = 0.0
    cpu_s: float = 0.0
    io_wait_s: float = 0.0

    io_ops: int = 0
    io_amplification: float = 0.0
    sift_steps_per_record: float = 0.0

    rss_growth_mb: float = 0.0
    verified: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def benchmark_engine(
    engine_name: str,
    input_path: str,
    output_path: str,
    scratch_dir: str,
    plan: MemoryPlan,
    distribution: str = "unknown",
    latency_ms: float = 0.0,
    input_summary: FileSummary | None = None,
) -> BenchmarkResult:
    if os.path.exists(scratch_dir):
        shutil.rmtree(scratch_dir)
    os.makedirs(scratch_dir, exist_ok=True)

    records = os.path.getsize(input_path) // RECORD_SIZE
    io_config = IOConfig(block_records=plan.block_records, latency_ms=latency_ms)
    stats = IOStats()

    engine = ENGINES[engine_name](scratch_dir, plan.capacity_records, io_config, stats)
    merger = CascadingKWayMerger(plan.fan_in, io_config, stats)

    result = BenchmarkResult(
        engine=engine_name,
        distribution=distribution,
        records=records,
        budget_mb=plan.budget_bytes / 1e6,
        capacity_records=plan.capacity_records,
        fan_in=plan.fan_in,
        latency_ms=latency_ms,
    )

    gc.collect()
    with MemoryProbe() as probe:
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        generation = engine.generate_runs(input_path)
        result.generation_s = time.perf_counter() - wall_start

        merge_start = time.perf_counter()
        merge = merger.merge(generation.run_paths, output_path)
        result.merge_s = time.perf_counter() - merge_start

        result.total_s = time.perf_counter() - wall_start
        result.cpu_s = time.process_time() - cpu_start

    result.io_wait_s = max(0.0, result.total_s - result.cpu_s)

    lengths = generation.run_lengths
    result.runs_generated = len(lengths)
    if lengths:
        steady = lengths[:-1] if len(lengths) > 1 else lengths
        result.run_multiplier = (sum(steady) / len(steady)) / plan.capacity_records

    result.merge_passes = merge.passes
    result.max_open_files = merge.max_open_files
    result.io_ops = stats.total_ops
    if records:
        result.io_amplification = (stats.bytes_read + stats.bytes_written) / (
            records * RECORD_SIZE
        )
        result.sift_steps_per_record = generation.sift_steps / records

    result.rss_growth_mb = max(0, probe.peak - probe.baseline) / 1e6

    summary = summarize_file(output_path)
    result.verified = summary.is_sorted and (
        input_summary is None
        or (summary.count, summary.checksum, summary.xor)
        == (input_summary.count, input_summary.checksum, input_summary.xor)
    )

    shutil.rmtree(scratch_dir, ignore_errors=True)
    return result


def crossover_latency_ms(knuth: BenchmarkResult, chunked: BenchmarkResult) -> float | None:
    """Per-I/O latency at which the two engines break even.

    Both cost roughly cpu + ops * latency. Knuth burns more CPU but issues
    fewer I/Os, so it wins once latency * (I/Os saved) exceeds the extra CPU.

    Returns None when Knuth did not remove a merge pass. Fewer run files on
    their own barely change the I/O count, so without a shallower merge tree
    there is nothing for a slow device to amplify and no latency can save it.
    """
    if knuth.merge_passes >= chunked.merge_passes:
        return None
    saved_ops = chunked.io_ops - knuth.io_ops
    if saved_ops <= 0:
        return None
    return max(0.0, (knuth.cpu_s - chunked.cpu_s) / saved_ops * 1000)
