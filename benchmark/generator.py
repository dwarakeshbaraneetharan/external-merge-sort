"""Synthetic 64-bit workloads.

uniform       random keys -- replacement selection should average 2M runs
presorted_90  mostly ascending with 10% local disorder, like an append log
reverse       strictly descending -- replacement selection's worst case
sorted        strictly ascending -- its best case, one run for the whole file
"""

from __future__ import annotations

import os
import random

from engine.io_channel import BinaryRunWriter, IOConfig, IOStats

DISTRIBUTIONS = ("uniform", "presorted_90", "reverse", "sorted")
MAX_KEY = (1 << 63) - 1


def generate_workload(path: str, num_records: int, distribution: str, seed: int = 42) -> str:
    if distribution not in DISTRIBUTIONS:
        raise ValueError(f"unknown distribution {distribution!r}")

    directory = os.path.dirname(path)
    if directory:  # a bare filename has no directory, and makedirs("") raises
        os.makedirs(directory, exist_ok=True)

    rng = random.Random(seed)
    batch = 65536
    with BinaryRunWriter(path, IOConfig(block_records=batch), IOStats()) as writer:
        written = 0
        trend = 1_000_000
        while written < num_records:
            count = min(batch, num_records - written)
            values, trend = _make_batch(distribution, count, rng, trend, written)
            writer.write_records(values)
            written += count
    return path


def _make_batch(distribution, count, rng, trend, already_written):
    """Returns (values, new_trend). The trend is carried across batches."""
    if distribution == "uniform":
        getrandbits = rng.getrandbits
        return [getrandbits(63) for _ in range(count)], trend

    if distribution == "reverse":
        start = MAX_KEY - already_written
        return [start - i for i in range(count)], trend

    # sorted / presorted_90 both walk a rising trend line
    values = []
    value = trend
    jitter = distribution == "presorted_90"
    for _ in range(count):
        value += rng.randrange(1, 20)
        # 10% of records jump backwards, the way a log with slightly
        # out-of-order timestamps would. The trend itself keeps rising, so the
        # sequence does not drift downwards over millions of records.
        if jitter and rng.random() < 0.10:
            values.append(max(0, value - rng.randrange(50, 5000)))
        else:
            values.append(value)
    return values, value
