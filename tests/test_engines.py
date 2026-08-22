"""Tests for the run generators, the merger, and the I/O layer.

The assertions that matter are the algorithmic invariants: every run comes out
sorted, no record is lost or duplicated, replacement selection really does reach
~2M run lengths on random data and collapse to 1M on reverse-sorted data, and
the cascading merge never opens more files than its fan-in allows.
"""

from __future__ import annotations

import math
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.generator import generate_workload
from benchmark.harness import (
    bytes_per_record,
    plan_memory,
    summarize_file,
)
from engine.chunked_sort import ChunkedSortEngine
from engine.io_channel import (
    RECORD_SIZE,
    BinaryRunReader,
    BinaryRunWriter,
    IOConfig,
    IOStats,
)
from engine.merger import CascadingKWayMerger
from engine.replacement_selection import ReplacementSelectionEngine, heapify, sift_down

ENGINES = [ReplacementSelectionEngine, ChunkedSortEngine]


def read_all(path):
    with BinaryRunReader(path) as reader:
        return list(reader.stream_all())


def write_all(path, values):
    with BinaryRunWriter(path) as writer:
        writer.write_records(values)
    return path


# --- heap ------------------------------------------------------------------

def test_heapify_matches_heapq():
    """Catches the sift-down bug of comparing children to an overwritten slot."""
    rng = random.Random(7)
    for _ in range(200):
        size = rng.randrange(1, 64)
        data = [rng.randrange(1000) for _ in range(size)]
        ours = list(data)
        heapify(ours, size)

        assert sorted(ours) == sorted(data)
        assert ours[0] == min(data)
        for parent in range(size):
            for child in (2 * parent + 1, 2 * parent + 2):
                if child < size:
                    assert ours[parent] <= ours[child]


def test_sift_down_stays_inside_the_active_heap():
    """The tail holds records parked for the next run and must not be touched."""
    buf = [5, 1, 3, 999, 998, 997]
    sift_down(buf, 0, 3)
    assert buf[3:] == [999, 998, 997]
    assert buf[0] == 1


def test_heap_pop_order_matches_heapq():
    rng = random.Random(11)
    data = [rng.randrange(10_000) for _ in range(500)]
    buf, size = list(data), len(data)
    heapify(buf, size)

    popped = []
    while size:
        popped.append(buf[0])
        size -= 1
        if size:
            buf[0] = buf[size]
            sift_down(buf, 0, size)
    assert popped == sorted(data)


# --- I/O -------------------------------------------------------------------

def test_roundtrip_with_partial_final_block(tmp_path):
    values = [random.getrandbits(63) for _ in range(8192 * 2 + 137)]
    path = write_all(str(tmp_path / "rt.bin"), values)
    assert os.path.getsize(path) == len(values) * RECORD_SIZE
    assert read_all(path) == values


def test_empty_file(tmp_path):
    path = write_all(str(tmp_path / "empty.bin"), [])
    assert read_all(path) == []
    with BinaryRunReader(path) as reader:
        assert reader.read_record() is None


def test_stats_count_blocks_not_records(tmp_path):
    stats = IOStats()
    with BinaryRunWriter(str(tmp_path / "s.bin"), IOConfig(block_records=100), stats) as writer:
        writer.write_records(range(250))
    assert stats.write_ops == 3  # 100 + 100 + 50
    assert stats.bytes_written == 250 * RECORD_SIZE


# --- run generation --------------------------------------------------------

@pytest.mark.parametrize("engine_class", ENGINES)
@pytest.mark.parametrize("distribution", ["uniform", "presorted_90", "reverse", "sorted"])
def test_runs_are_sorted_and_lossless(tmp_path, engine_class, distribution):
    source = str(tmp_path / "in.bin")
    generate_workload(source, 20_000, distribution, seed=11)

    engine = engine_class(str(tmp_path / "runs"), 1000)
    result = engine.generate_runs(source)

    emitted = []
    for path in result.run_paths:
        run = read_all(path)
        assert run == sorted(run), "run came out unsorted"
        emitted.extend(run)

    assert sorted(emitted) == sorted(read_all(source))
    assert sum(result.run_lengths) == 20_000


def test_replacement_selection_averages_two_M_on_random_data(tmp_path):
    """Knuth's headline result: E[run length] = 2M on uniformly random input."""
    source = str(tmp_path / "u.bin")
    generate_workload(source, 100_000, "uniform", seed=3)
    capacity = 5_000

    result = ReplacementSelectionEngine(str(tmp_path / "runs"), capacity).generate_runs(source)

    # Drop the final run: it is cut short by end of input, not by the algorithm.
    steady = result.run_lengths[:-1]
    multiplier = (sum(steady) / len(steady)) / capacity
    assert 1.8 <= multiplier <= 2.2, f"expected ~2.0x M, got {multiplier:.2f}x"


def test_replacement_selection_makes_one_run_when_input_is_sorted(tmp_path):
    source = str(tmp_path / "s.bin")
    generate_workload(source, 50_000, "sorted", seed=5)
    result = ReplacementSelectionEngine(str(tmp_path / "runs"), 1000).generate_runs(source)
    assert result.run_lengths == [50_000]


def test_replacement_selection_collapses_to_M_on_reverse_input(tmp_path):
    """Worst case: every record is deferred, so runs are exactly M."""
    source = str(tmp_path / "r.bin")
    generate_workload(source, 20_000, "reverse", seed=5)
    result = ReplacementSelectionEngine(str(tmp_path / "runs"), 1000).generate_runs(source)
    assert result.run_lengths == [1000] * 20


def test_chunked_runs_are_exactly_M(tmp_path):
    source = str(tmp_path / "u.bin")
    generate_workload(source, 20_500, "uniform", seed=5)
    result = ChunkedSortEngine(str(tmp_path / "runs"), 1000).generate_runs(source)
    assert result.run_count == math.ceil(20_500 / 1000)
    assert result.run_lengths == [1000] * 20 + [500]


@pytest.mark.parametrize("engine_class", ENGINES)
@pytest.mark.parametrize("records", [0, 1, 999, 1000, 1001])
def test_inputs_smaller_than_one_buffer(tmp_path, engine_class, records):
    source = str(tmp_path / f"n{records}.bin")
    generate_workload(source, records, "uniform", seed=9)
    result = engine_class(str(tmp_path / "runs"), 1000).generate_runs(source)

    emitted = []
    for path in result.run_paths:
        emitted.extend(read_all(path))
    assert sorted(emitted) == sorted(read_all(source))
    if records == 0:
        assert result.run_count == 0


# --- merge -----------------------------------------------------------------

def test_cascading_merge_respects_fan_in(tmp_path):
    """20 runs with fan-in 3 has to cascade, never opening more than 3 at once."""
    rng = random.Random(2)
    expected, run_paths = [], []
    for index in range(20):
        values = sorted(rng.randrange(10_000) for _ in range(50))
        expected.extend(values)
        run_paths.append(write_all(str(tmp_path / f"run{index}.bin"), values))

    merger = CascadingKWayMerger(max_fan_in=3)
    output = str(tmp_path / "merged.bin")
    result = merger.merge(run_paths, output)

    assert read_all(output) == sorted(expected)
    assert result.max_open_files <= 3
    assert result.passes == merger.expected_passes(20) == math.ceil(math.log(20, 3))
    # Every intermediate generation should be cleaned up.
    assert os.listdir(tmp_path) == ["merged.bin"]


def test_single_run_skips_the_merge(tmp_path):
    """Replacement selection on presorted input: zero merge passes."""
    values = sorted(random.randrange(1000) for _ in range(500))
    run = write_all(str(tmp_path / "run0.bin"), values)
    output = str(tmp_path / "out.bin")

    result = CascadingKWayMerger(max_fan_in=8).merge([run], output)

    assert result.passes == 0
    assert read_all(output) == values
    assert not os.path.exists(run)


def test_merging_nothing_gives_an_empty_file(tmp_path):
    output = str(tmp_path / "out.bin")
    CascadingKWayMerger(max_fan_in=4).merge([], output)
    assert os.path.getsize(output) == 0


# --- end to end ------------------------------------------------------------

@pytest.mark.parametrize("engine_class", ENGINES)
@pytest.mark.parametrize("distribution", ["uniform", "presorted_90", "reverse"])
def test_sorted_output_keeps_every_record(tmp_path, engine_class, distribution):
    source = str(tmp_path / "in.bin")
    generate_workload(source, 30_000, distribution, seed=13)
    before = summarize_file(source)

    generation = engine_class(str(tmp_path / "runs"), 700).generate_runs(source)
    output = str(tmp_path / "sorted.bin")
    CascadingKWayMerger(max_fan_in=4).merge(generation.run_paths, output)

    after = summarize_file(output)
    assert after.is_sorted
    assert (after.count, after.checksum, after.xor) == (before.count, before.checksum, before.xor)


def test_both_engines_produce_identical_output(tmp_path):
    source = str(tmp_path / "in.bin")
    generate_workload(source, 25_000, "uniform", seed=17)

    outputs = []
    for index, engine_class in enumerate(ENGINES):
        generation = engine_class(str(tmp_path / f"runs{index}"), 900).generate_runs(source)
        output = str(tmp_path / f"out{index}.bin")
        CascadingKWayMerger(max_fan_in=5).merge(generation.run_paths, output)
        outputs.append(read_all(output))

    assert outputs[0] == outputs[1]


# --- memory planning -------------------------------------------------------

def test_derived_record_size_matches_real_memory():
    """The budget maths is derived, not sampled, so check it against RSS once.

    In a fresh interpreter: by this point in the test session the allocator is
    holding freed arenas, so a new list of ints reuses them and costs almost no
    RSS, which reads as ~36 B/record instead of 56.
    """
    root = Path(__file__).resolve().parents[1]
    probe = subprocess.run(
        [sys.executable, "-c",
         "from benchmark.harness import measure_bytes_per_record as m; print(m(400_000))"],
        cwd=root, capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr

    derived = bytes_per_record()
    measured = float(probe.stdout.strip())
    assert derived >= 40, "a list slot plus an integer object cannot be this small"
    assert abs(derived - measured) / measured < 0.20, (
        f"derived {derived:.1f} B/record but measured {measured:.1f} B/record"
    )


def test_fan_in_is_clamped_when_read_buffers_would_not_fit():
    plan = plan_memory(2_000_000, bytes_per_record=56.0, block_records=8192, fan_in=64)
    assert plan.fan_in_clamped
    assert (plan.fan_in + 1) * 8192 * 56.0 <= 2_000_000 * 0.9


def test_capacity_leaves_room_for_io_buffers():
    plan = plan_memory(8_000_000, bytes_per_record=56.0, block_records=8192, fan_in=8)
    assert plan.capacity_records == int(8_000_000 / 56.0) - 2 * 8192
    assert plan.run_phase_bytes <= 8_000_000
