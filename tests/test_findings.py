"""The README's claims, as assertions.

Wall clock is machine-specific, so nothing here asserts a time. What the README
actually claims is structural: how many runs each engine produces, how deep the
merge tree gets, and how many bytes move. Those follow from the algorithms, the
memory budget and the fan-in rather than from the hardware, so they should hold
on any machine, including CI.

The fan-in deliberately sits between the two engines' run counts. That is the
only regime where replacement selection's shorter run list can remove a merge
pass, so showing it requires setting it up on purpose.
"""

from __future__ import annotations

import pytest

from benchmark.generator import generate_workload
from benchmark.harness import benchmark_engine, plan_memory, summarize_file

RECORDS = 400_000
BUDGET = 2_000_000
BLOCK = 1024
FAN_IN = 8
DISTRIBUTIONS = ("uniform", "presorted_90", "reverse")


@pytest.fixture(scope="module")
def measured(tmp_path_factory):
    """Sort every distribution with both engines once, then reuse the results."""
    root = tmp_path_factory.mktemp("findings")
    plan = plan_memory(BUDGET, bytes_per_record=56.0, block_records=BLOCK, fan_in=FAN_IN)

    results = {}
    for distribution in DISTRIBUTIONS:
        source = str(root / f"{distribution}.bin")
        generate_workload(source, RECORDS, distribution, seed=42)
        summary = summarize_file(source)
        pair = {
            engine: benchmark_engine(
                engine,
                source,
                str(root / f"out_{engine}.bin"),
                str(root / f"scratch_{engine}"),
                plan,
                distribution=distribution,
                input_summary=summary,
            )
            for engine in ("knuth", "chunked")
        }
        results[distribution] = (pair["knuth"], pair["chunked"])
    return results


def test_both_engines_sort_correctly(measured):
    """Nothing below means anything if the output is wrong."""
    for distribution in DISTRIBUTIONS:
        knuth, chunked = measured[distribution]
        assert knuth.verified, f"replacement selection failed on {distribution}"
        assert chunked.verified, f"chunked sort failed on {distribution}"


def test_replacement_selection_halves_the_run_count(measured):
    """The 2M result: on random input, runs average twice the buffer."""
    knuth, chunked = measured["uniform"]

    assert 1.7 <= knuth.run_multiplier <= 2.3, (
        f"expected ~2.0x M, got {knuth.run_multiplier:.2f}x"
    )
    assert chunked.run_multiplier == pytest.approx(1.0, abs=0.01)
    assert knuth.runs_generated < chunked.runs_generated


def test_fewer_runs_can_remove_a_merge_pass(measured):
    """The I/O saving is real, when the fan-in lets it happen."""
    knuth, chunked = measured["uniform"]

    assert knuth.runs_generated <= FAN_IN < chunked.runs_generated
    assert knuth.merge_passes < chunked.merge_passes
    assert knuth.io_amplification < chunked.io_amplification


def test_presorted_input_needs_no_merge_at_all(measured):
    """Best case: one run covering the file, so the merge phase disappears."""
    knuth, chunked = measured["presorted_90"]

    assert knuth.runs_generated == 1
    assert knuth.merge_passes == 0
    assert knuth.io_amplification == pytest.approx(2.0, abs=0.1)
    assert chunked.merge_passes > 0


def test_reverse_input_makes_the_advantage_vanish(measured):
    """Worst case: every record is parked at once, so runs collapse to exactly M.

    Identical run counts, identical merge depth, identical bytes moved. The heap
    cost is paid for nothing, which is the sharp end of the finding.
    """
    knuth, chunked = measured["reverse"]

    assert knuth.run_multiplier == pytest.approx(1.0, abs=0.01)
    assert knuth.runs_generated == chunked.runs_generated
    assert knuth.merge_passes == chunked.merge_passes
    assert knuth.io_amplification == pytest.approx(chunked.io_amplification, abs=0.01)


def test_replacement_selection_does_the_extra_cpu_work(measured):
    """Where the time goes: ~15 dependent memory hops per record, against none."""
    knuth, chunked = measured["uniform"]

    assert knuth.sift_steps_per_record > 10
    assert chunked.sift_steps_per_record == 0
