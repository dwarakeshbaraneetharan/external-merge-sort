"""Entry point.

    python cli.py bench --records 3000000 --budget-mb 8
    python cli.py crossover --records 1000000 --budget-mb 2 --fan-in 20
    python cli.py memtrap
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from benchmark.generator import DISTRIBUTIONS, generate_workload
from benchmark.harness import (
    benchmark_engine,
    bytes_per_record,
    crossover_latency_ms,
    plan_memory,
    summarize_file,
)
from engine.io_channel import RECORD_SIZE

DATA_DIR, RESULTS_DIR, SCRATCH_DIR = "data", "results", "scratch"

COLUMNS = [
    ("engine", lambda r: r.engine),
    ("dist", lambda r: r.distribution),
    ("runs", lambda r: f"{r.runs_generated:,}"),
    ("run/M", lambda r: f"{r.run_multiplier:.2f}x"),
    ("passes", lambda r: str(r.merge_passes)),
    ("sift/rec", lambda r: f"{r.sift_steps_per_record:.1f}"),
    ("gen s", lambda r: f"{r.generation_s:.2f}"),
    ("merge s", lambda r: f"{r.merge_s:.2f}"),
    ("total s", lambda r: f"{r.total_s:.2f}"),
    ("io wait s", lambda r: f"{r.io_wait_s:.2f}"),
    ("io amp", lambda r: f"{r.io_amplification:.1f}x"),
    ("rss MB", lambda r: f"{r.rss_growth_mb:.1f}"),
    ("ok", lambda r: "yes" if r.verified else "NO"),
]


def render_table(headers, rows):
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join(lines)


def results_table(results):
    return render_table([name for name, _ in COLUMNS],
                        [[get(r) for _, get in COLUMNS] for r in results])


def ensure_dataset(distribution, records, seed, quiet=False):
    path = os.path.join(DATA_DIR, f"{distribution}_{records}_{seed}.bin")
    if os.path.exists(path) and os.path.getsize(path) == records * RECORD_SIZE:
        return path
    if not quiet:
        print(f"  generating {records:,} records ({records * RECORD_SIZE / 1e6:.0f} MB) "
              f"[{distribution}] ...")
    start = time.perf_counter()
    generate_workload(path, records, distribution, seed=seed)
    if not quiet:
        print(f"  done in {time.perf_counter() - start:.1f}s")
    return path


def run_matrix(args, distributions, latency_ms, record_bytes, quiet=False):
    plan = plan_memory(int(args.budget_mb * 1e6), record_bytes,
                       args.block_records, args.fan_in)
    if not quiet:
        print(plan.describe())
        if plan.fan_in_clamped:
            print(f"  fan-in clamped to {plan.fan_in}: read buffers for the requested "
                  f"fan-in do not fit the budget")

    results = []
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for distribution in distributions:
        path = ensure_dataset(distribution, args.records, args.seed, quiet)
        summary = summarize_file(path)
        for engine in ("knuth", "chunked"):
            if not quiet:
                print(f"  {engine:8s} {distribution:13s}", end="", flush=True)
            output = os.path.join(RESULTS_DIR, f"sorted_{engine}.bin")
            result = benchmark_engine(engine, path, output,
                                      os.path.join(SCRATCH_DIR, engine), plan,
                                      distribution, latency_ms, summary)
            os.remove(output)
            results.append(result)
            if not quiet:
                print(f"{result.total_s:8.2f}s  {result.runs_generated:>4} runs  "
                      f"{result.merge_passes} passes  "
                      f"{'verified' if result.verified else 'FAILED'}")
    return results


def summarize(results):
    lines = []
    for distribution in dict.fromkeys(r.distribution for r in results):
        pair = {r.engine: r for r in results if r.distribution == distribution}
        knuth, chunked = pair.get("knuth"), pair.get("chunked")
        if not knuth or not chunked:
            continue
        ratio = chunked.total_s / knuth.total_s
        winner = "knuth" if ratio > 1 else "chunked"
        crossover = crossover_latency_ms(knuth, chunked)
        note = (f"knuth breaks even at {crossover:.2f} ms/IO" if crossover
                else "same merge depth, so knuth saves nothing at any latency")
        lines.append(
            f"{distribution:<13} runs {knuth.runs_generated:>3} vs {chunked.runs_generated:<3} "
            f"| passes {knuth.merge_passes} vs {chunked.merge_passes} "
            f"| I/O {knuth.io_amplification:.1f}x vs {chunked.io_amplification:.1f}x "
            f"| {winner} wins by {max(ratio, 1 / ratio):.2f}x, {note}"
        )
    return "\n".join(lines)


def cmd_bench(args):
    record_bytes = bytes_per_record()
    print(f"one buffered record costs {record_bytes:.0f} bytes in CPython "
          f"(8 byte list slot + integer object)\n")
    results = run_matrix(args, args.distributions, args.latency_ms, record_bytes)

    print("\n" + results_table(results) + "\n")
    print(summarize(results))

    tag = f"{args.records}rec_{args.budget_mb:g}mb"
    with open(os.path.join(RESULTS_DIR, f"bench_{tag}.json"), "w") as handle:
        json.dump([r.as_dict() for r in results], handle, indent=2)

    if not args.no_plot:
        from benchmark.plots import plot_benchmark

        subtitle = (f"{args.records:,} records ({args.records * 8 / 1e6:.0f} MB), "
                    f"{args.budget_mb:g} MB budget, fan-in {results[0].fan_in}")
        print("\nwrote " + plot_benchmark(results, os.path.join(RESULTS_DIR, f"bench_{tag}.png"),
                                          subtitle))
    return 0


def cmd_crossover(args):
    record_bytes = bytes_per_record()
    print(f"sweeping simulated I/O latency on '{args.distribution}'\n")

    rows, table, baseline = [], [], {}
    for latency in args.latencies:
        results = run_matrix(args, [args.distribution], latency, record_bytes, quiet=True)
        knuth = next(r for r in results if r.engine == "knuth")
        chunked = next(r for r in results if r.engine == "chunked")
        if latency == 0:
            baseline = {"knuth": knuth, "chunked": chunked}
        ratio = chunked.total_s / knuth.total_s
        rows.append((latency, knuth.total_s, chunked.total_s))
        table.append([f"{latency:g}", f"{knuth.total_s:.2f}", f"{chunked.total_s:.2f}",
                      f"{ratio:.2f}x", "knuth" if ratio > 1 else "chunked",
                      f"{knuth.runs_generated}/{chunked.runs_generated}",
                      f"{knuth.merge_passes}/{chunked.merge_passes}",
                      f"{knuth.io_ops:,}", f"{chunked.io_ops:,}"])
        print(f"  {latency:>5g} ms   knuth {knuth.total_s:7.2f}s   "
              f"chunked {chunked.total_s:7.2f}s   -> {'knuth' if ratio > 1 else 'chunked'}")

    headers = ["latency ms", "knuth s", "chunked s", "ratio", "winner",
               "runs k/c", "passes k/c", "knuth ios", "chunked ios"]
    print("\n" + render_table(headers, table))

    crossover = None
    if baseline:
        crossover = crossover_latency_ms(baseline["knuth"], baseline["chunked"])
        if crossover:
            print(f"\npredicted break-even: {crossover:.2f} ms per I/O")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    tag = f"{args.distribution}_{args.records}"
    with open(os.path.join(RESULTS_DIR, f"crossover_{tag}.json"), "w") as handle:
        json.dump({"sweep": rows, "break_even_ms": crossover,
                   "baseline": {k: v.as_dict() for k, v in baseline.items()}}, handle, indent=2)

    if not args.no_plot and baseline:
        from benchmark.plots import plot_crossover

        subtitle = (f"{args.records:,} records, {args.budget_mb:g} MB budget, "
                    f"fan-in {baseline['knuth'].fan_in}, {args.distribution}")
        print("wrote " + plot_crossover(rows, baseline["knuth"], baseline["chunked"], crossover,
                                        os.path.join(RESULTS_DIR, f"crossover_{tag}.png"),
                                        subtitle))
    return 0


def cmd_memtrap(args):
    """Show why the sort buffer is not sized as budget // 8."""
    import array
    import gc
    import random

    from benchmark.harness import current_rss

    count = args.records
    # The ints have to be created inside the measurement; copying an existing
    # list only allocates pointers and hides the PyLongObject behind each one.
    gc.collect()
    before = current_rss()
    rng = random.Random(1)
    as_list = [rng.getrandbits(63) for _ in range(count)]
    list_bytes = current_rss() - before
    int_size = sys.getsizeof(as_list[0])
    del as_list
    gc.collect()

    before = current_rss()
    rng = random.Random(1)
    packed = array.array("Q", (rng.getrandbits(63) for _ in range(count)))
    array_bytes = current_rss() - before
    del packed
    gc.collect()

    on_disk = count * RECORD_SIZE
    print(f"{count:,} 64-bit keys")
    for label, size in (("packed on disk", on_disk), ("python list of ints", list_bytes),
                        ("array.array('Q')", array_bytes)):
        print(f"  {label:<22}{size / 1e6:8.2f} MB   {size / count:5.1f} bytes/record")
    print(f"  sys.getsizeof(one int){int_size:8d} bytes  (+8 for the list slot)")
    print(f"\nA list buffer costs {list_bytes / on_disk:.1f}x its packed size, which is why the "
          f"harness measures this instead of assuming 8 bytes.")
    return 0


def cmd_clean(args):
    for directory in (DATA_DIR, SCRATCH_DIR):
        if os.path.exists(directory):
            shutil.rmtree(directory)
            print(f"removed {directory}/")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def shared(subparser, records):
        subparser.add_argument("--records", type=int, default=records)
        subparser.add_argument("--budget-mb", type=float, default=8.0)
        subparser.add_argument("--fan-in", type=int, default=32)
        subparser.add_argument("--block-records", type=int, default=8192)
        subparser.add_argument("--seed", type=int, default=42)
        subparser.add_argument("--no-plot", action="store_true")

    bench = subparsers.add_parser("bench", help="both engines across every distribution")
    bench.add_argument("--distributions", nargs="+", choices=DISTRIBUTIONS,
                       default=["uniform", "presorted_90", "reverse"])
    bench.add_argument("--latency-ms", type=float, default=0.0)
    shared(bench, 3_000_000)
    bench.set_defaults(func=cmd_bench)

    crossover = subparsers.add_parser("crossover", help="sweep simulated storage latency")
    crossover.add_argument("--distribution", choices=DISTRIBUTIONS, default="uniform")
    crossover.add_argument("--latencies", nargs="+", type=float, default=[0, 1, 2, 4, 8])
    shared(crossover, 1_000_000)
    crossover.set_defaults(func=cmd_crossover)

    memtrap = subparsers.add_parser("memtrap", help="measure CPython's per-record overhead")
    memtrap.add_argument("--records", type=int, default=1_000_000)
    memtrap.set_defaults(func=cmd_memtrap)

    subparsers.add_parser("clean", help="delete generated data").set_defaults(func=cmd_clean)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
