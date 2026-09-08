"""Charts for the benchmark results. Requires matplotlib."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORS = {"knuth": "#c1440e", "chunked": "#2b6cb0"}
LABELS = {"knuth": "Replacement selection", "chunked": "Chunked sort"}
# Typical per-access latency, for orientation on the crossover chart.
DEVICES = [(0.02, "NVMe Gen4"), (0.1, "SATA SSD"), (8.0, "7200 RPM HDD")]


def _grouped_bars(axis, distributions, results, value_of, ylabel, title, fmt, log=False):
    width = 0.38
    peak = 0
    for offset, engine in enumerate(("knuth", "chunked")):
        values = []
        for distribution in distributions:
            match = [r for r in results if r.distribution == distribution and r.engine == engine]
            values.append(value_of(match[0]) if match else 0)
        peak = max(peak, *values)
        positions = [i + offset * width for i in range(len(distributions))]
        bars = axis.bar(positions, values, width, label=LABELS[engine], color=COLORS[engine])
        axis.bar_label(bars, fmt=fmt, fontsize=8, padding=2)

    if log:
        axis.set_yscale("log")
        axis.set_ylim(0.7, peak * 3)
    else:
        axis.set_ylim(0, peak * 1.18)  # headroom so the bar labels are not clipped
    axis.set_xticks([i + width / 2 for i in range(len(distributions))])
    axis.set_xticklabels(distributions, fontsize=9)
    axis.set_ylabel(ylabel, fontsize=9)
    axis.set_title(title, fontsize=10, fontweight="bold")
    axis.grid(axis="y", alpha=0.25)
    axis.set_axisbelow(True)


def plot_benchmark(results, path: str, subtitle: str = "") -> str:
    distributions = list(dict.fromkeys(r.distribution for r in results))

    figure, axes = plt.subplots(2, 2, figsize=(11, 8))
    _grouped_bars(axes[0][0], distributions, results, lambda r: r.total_s,
                  "seconds", "Total wall clock", "{:.2f}")
    _grouped_bars(axes[0][1], distributions, results, lambda r: r.runs_generated,
                  "run files", "Runs generated", "{:.0f}")
    _grouped_bars(axes[1][0], distributions, results, lambda r: r.run_multiplier,
                  "run length / M (log)", "Run length vs memory size", "{:.2f}x", log=True)
    _grouped_bars(axes[1][1], distributions, results, lambda r: r.io_amplification,
                  "bytes moved / input", "Total I/O", "{:.1f}x")

    # Knuth's prediction for uniformly random input.
    reference = axes[1][0]
    reference.axhline(2.0, color="#444", linestyle="--", linewidth=1.2)
    reference.text(0.99, 2.15, "Knuth predicts 2.0x on random input", fontsize=8, color="#444",
                   ha="right", transform=reference.get_yaxis_transform())

    handles, labels = axes[0][0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", ncol=2, fontsize=9,
                  bbox_to_anchor=(0.5, 0.925), frameon=False)

    title = "Replacement selection vs chunked sorting"
    figure.suptitle(f"{title}\n{subtitle}" if subtitle else title, fontsize=13, fontweight="bold")
    figure.tight_layout(rect=(0, 0, 1, 0.9))
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path


def plot_crossover(rows, knuth, chunked, crossover_ms, path: str, subtitle: str = "") -> str:
    """rows: (latency_ms, knuth_seconds, chunked_seconds) as measured."""
    figure, (left, right) = plt.subplots(1, 2, figsize=(12, 5))
    latencies = [row[0] for row in rows]

    # Left: what was actually measured.
    if crossover_ms:
        left.axvspan(0, crossover_ms, color=COLORS["chunked"], alpha=0.07)
        left.axvspan(crossover_ms, max(latencies), color=COLORS["knuth"], alpha=0.07)
    left.plot(latencies, [row[1] for row in rows], "o-", color=COLORS["knuth"],
              label=LABELS["knuth"], linewidth=2)
    left.plot(latencies, [row[2] for row in rows], "s-", color=COLORS["chunked"],
              label=LABELS["chunked"], linewidth=2)
    left.set_xlim(0, max(latencies))
    left.text(0.97, 0.06, "replacement selection wins here", transform=left.transAxes,
              fontsize=8.5, color=COLORS["knuth"], ha="right", style="italic")
    left.set_xlabel("simulated latency per I/O (ms)", fontsize=9)
    left.set_ylabel("total wall clock (s)", fontsize=9)
    left.set_title("Measured", fontsize=10, fontweight="bold")
    left.legend(fontsize=8.5, loc="upper left")
    left.grid(alpha=0.25)
    left.set_axisbelow(True)

    # Right: time = cpu + ops * latency, extended across a latency range that
    # time.sleep() is too coarse to measure directly.
    grid = [0.01 * (1.15 ** i) for i in range(62)]
    for result, key in ((knuth, "knuth"), (chunked, "chunked")):
        right.plot(grid, [result.cpu_s + result.io_ops * ms / 1000 for ms in grid],
                   color=COLORS[key], label=LABELS[key], linewidth=2)
    right.set_xscale("log")
    right.set_yscale("log")

    for latency, name in DEVICES:
        right.axvline(latency, color="#999", linestyle="--", linewidth=0.9)
        right.text(latency, 0.02, f" {name}", transform=right.get_xaxis_transform(),
                   fontsize=7.5, color="#555", rotation=90, va="bottom")

    if crossover_ms:
        y = knuth.cpu_s + knuth.io_ops * crossover_ms / 1000
        right.plot([crossover_ms], [y], "o", color="#111", markersize=7, zorder=5)
        right.annotate(f"break-even\n{crossover_ms:.2f} ms/IO", xy=(crossover_ms, y),
                       xytext=(14, -34), textcoords="offset points", fontsize=9,
                       fontweight="bold", arrowprops=dict(arrowstyle="->", color="#111"))

    right.set_xlabel("latency per I/O (ms, log scale)", fontsize=9)
    right.set_ylabel("modelled total time (s, log scale)", fontsize=9)
    right.set_title("Modelled across storage generations", fontsize=10, fontweight="bold")
    right.legend(fontsize=8.5, loc="upper left")
    right.grid(alpha=0.25, which="both")
    right.set_axisbelow(True)

    title = "Where replacement selection starts winning again"
    figure.suptitle(f"{title}\n{subtitle}" if subtitle else title, fontsize=13, fontweight="bold")
    figure.tight_layout(rect=(0, 0, 1, 0.9))
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return path
