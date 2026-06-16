#!/usr/bin/env python3
"""Auto-generate the per-environment cardano-node stats report (the
InMemory-vs-LSM comparison documented in node-report-template.md).

For each environment it reads that env's SQLite stats DB
(data/cardano-node/<env>.db) and, for each build present, assembles the CPU &
RAM, Ingest, On-disk Size, and RTS plots - the figures node-plot.py already
builds - then a comparison section. Two renderings are produced (see --format):
a Markdown file with auto-rendered PNGs (no more clicking Plotly's camera button)
and a self-contained interactive HTML. SQLite-read-only; never touches Postgres.

This is the cardano-node sibling of db-sync-stats-report.py and shares the same
machinery (scripts/_report.py). It is unrelated to the Postgres-connected
db-sync-epoch-report.py.

A few node-specific points:
  - Ingest is plotted on an epoch x-axis (sync time by era + per-epoch duration),
    so it is rendered once per build rather than on both slot and time axes.
  - CPU & RAM, Disk, and RTS are rendered on both the slot and time axes.
  - Disk and RTS come from optional separate collectors (node-db-size-monitor.py,
    node-rts-monitor.py); a DB that never ran them simply omits those plots.

Examples:
    # One env, both builds, both output formats (default):
    python scripts/node-stats-report.py --env preprod \\
        --inmemory 11.0.1 --lsm LSM-11.0.1

    # All envs, add a previous-version comparison, HTML only (no kaleido needed):
    python scripts/node-stats-report.py --env all --format html \\
        --inmemory 11.0.1 --lsm LSM-11.0.1 --compare-to 10.1.4
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from _common import (
    VERSION_KEYED_TABLES,
    compute_epoch_durations,
    has_table,
    load_all_versions,
    resolve_versions,
    short,
    warn,
)
from _report import (
    KaleidoMissingError,
    ReportImage,
    ReportSection,
    assemble_html,
    assemble_markdown,
    render_png,
)
from plotly.graph_objs import Figure

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "cardano-node"
DEFAULT_OUT_DIR = PROJECT_ROOT / "reports" / "cardano-node"
ENVS = ("mainnet", "preprod", "preview")

# (kind, caption, filename infix, axes). Order = report order. Ingest is
# epoch-axis only (the era/per-epoch figure ignores slot/time), so it renders
# once; the rest render on both slot and time. disk/rts come from optional
# collectors and are skipped (build_fig returns None) when not collected.
METRICS = [
    ("cpu_ram", "CPU & RAM (RSS)", "", ("slot", "time")),
    ("ingest", "Ingest Metrics", "ingest-metrics-", ("epoch",)),
    ("disk", "On-disk DB Size", "disk-", ("slot", "time")),
    ("rts", "RTS / Runtime Metrics", "rts-", ("slot", "time")),
]
# Comparison sections overlay runs that happened at different wall-clock times,
# so they align by chain position: slot for cpu_ram/disk, epoch for ingest. RTS
# is left out of the overlays - it is many panels and reads better per-build.
HEADLINE = [
    ("cpu_ram", "CPU & RAM (RSS)", "", "slot"),
    ("ingest", "Ingest Metrics", "ingest-metrics-", "epoch"),
    ("disk", "On-disk DB Size", "disk-", "slot"),
]


def _load_nodeplot():
    """Import the hyphenated sibling node-plot.py as a module."""
    spec = importlib.util.spec_from_file_location("node_plot", Path(__file__).with_name("node-plot.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["node_plot"] = mod  # so any future-annotation dataclasses resolve
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


nodeplot = _load_nodeplot()


def _ingest_per_epoch(db: str, versions: list[str]):
    """Load node_ingest_metrics for the versions that actually have rows, then
    reduce to per-(version, epoch) durations - the shape build_ingest wants.
    Returns an empty frame when nothing is plottable."""
    df = nodeplot.load_node_ingest(db, versions)
    if df.empty:
        return df
    present = [v for v in versions if v in set(df["version"].unique())]
    if not present:
        return df.iloc[0:0]
    return compute_epoch_durations(df[df["version"].isin(present)])


def build_fig(db: str, versions: list[str], env: str, kind: str, x_axis: str) -> Figure | None:
    """Load + build one metric figure, or None if the data isn't plottable on
    this axis (e.g. a pre-ts DB on the time axis, or an optional collector's
    table that was never written) - reported, never fatal."""
    try:
        if kind == "cpu_ram":
            mem_df, cpu_df = nodeplot.load_cpu_ram(db, versions, x_axis)
            if mem_df.empty and cpu_df.empty:
                return None
            return nodeplot.build_cpu_ram(mem_df, cpu_df, versions, env, x_axis)
        if kind == "ingest":
            if not has_table(db, "node_ingest_metrics"):
                return None
            per_epoch = _ingest_per_epoch(db, versions)
            return None if per_epoch.empty else nodeplot.build_ingest(per_epoch, versions, env, x_axis)
        if kind == "disk":
            if not has_table(db, "disk_metrics"):
                return None
            df = nodeplot.load_disk(db, versions, x_axis)
            return None if df.empty else nodeplot.build_disk(df, versions, env, x_axis)
        if kind == "rts":
            if not has_table(db, "rts_metrics"):
                return None
            df = nodeplot.load_rts(db, versions, x_axis)
            return None if df.empty else nodeplot.build_rts(df, versions, env, x_axis)
    except SystemExit as e:  # load_cpu_ram raises this for a pre-ts DB on time axis
        warn(f"skipped {kind} ({x_axis}-axis): {e}")
    except Exception as e:
        warn(f"skipped {kind} ({x_axis}-axis): {e}")
    return None


def _png_name(env: str, tag: str, infix: str, x_axis: str) -> str:
    return f"cardano-node-{env}-{tag}-{infix}{x_axis}-axis.png"


def build_build_section(db: str, env: str, label: str, role: str) -> ReportSection:
    """One build's section: every metric on the axes it supports."""
    tag = short(label)
    sec = ReportSection(title=f"{role} version ({tag})")
    for kind, caption, infix, axes in METRICS:
        for axis in axes:
            # Building a figure (especially RTS on mainnet) can take a while, so
            # announce each one - otherwise the run looks hung during this phase.
            print(f"[{env}] building {role} ({tag}): {caption} - {axis}-axis…", flush=True)
            fig = build_fig(db, [label], env, kind, axis)
            if fig is None:
                print(f"[{env}]   -> no data for this metric/axis, skipped", flush=True)
                continue
            sec.images.append(
                ReportImage(
                    caption=f"{caption} - {axis}-axis",
                    png_name=_png_name(env, tag, infix, axis),
                    fig=fig,
                )
            )
    return sec


def build_comparison_section(db: str, env: str, labels: list[str], title: str, tag: str) -> ReportSection:
    """Overlay the given build labels for the headline metrics.

    Uses the **slot** axis for cpu_ram/disk and the **epoch** axis for ingest:
    the runs happened at different wall-clock times, so a time axis would shift
    the curves apart and make the comparison meaningless. Slot number / epoch are
    the shared chain position, so they align the runs.
    """
    sec = ReportSection(title=title)
    for kind, caption, infix, axis in HEADLINE:
        print(f"[{env}] building {title}: {caption} - {axis}-axis…", flush=True)
        fig = build_fig(db, labels, env, kind, axis)
        if fig is None:
            print(f"[{env}]   -> no data for this metric/axis, skipped", flush=True)
            continue
        sec.images.append(
            ReportImage(
                caption=f"{caption} - {axis}-axis",
                png_name=_png_name(env, tag, infix, axis),
                fig=fig,
            )
        )
    return sec


def _resolve(token: str | None, available: list[str]) -> str | None:
    if not token:
        return None
    try:
        return resolve_versions([token], available)[0]
    except SystemExit as e:
        warn(str(e))
        return None


def build_env_report(db: str, env: str, args: argparse.Namespace) -> list[ReportSection] | None:
    """Assemble the section list for one environment, or None if no build resolved."""
    available = load_all_versions(db, list(VERSION_KEYED_TABLES["node"]))
    if not available:
        warn(f"{env}: no versions found in {db}; skipping.")
        return None

    inmem = _resolve(args.inmemory, available)
    lsm = _resolve(args.lsm, available)
    if not inmem and not lsm:
        warn(f"{env}: neither --inmemory nor --lsm resolved against {db}; skipping.")
        return None

    print(f"[{env}] loading data and building figures…", flush=True)
    sections: list[ReportSection] = []
    if inmem:
        sections.append(build_build_section(db, env, inmem, "InMemory"))
    if lsm:
        sections.append(build_build_section(db, env, lsm, "LSM"))

    # A. LSM vs InMemory (same version).
    if inmem and lsm:
        sections.append(
            build_comparison_section(db, env, [inmem, lsm], "LSM vs InMemory Comparison", "LSM-vs-InMemory")
        )
    elif args.lsm and args.inmemory:
        print(f"{env}: only one of the two builds is present; skipping LSM-vs-InMemory comparison.")

    # B. This version vs a previous version (current builds overlaid with prev).
    prev = _resolve(args.compare_to, available)
    if prev:
        current = [v for v in (inmem, lsm) if v]
        sections.append(
            build_comparison_section(db, env, [*current, prev], f"This vs Previous ({short(prev)})", "this-vs-previous")
        )
    elif args.compare_to:
        print(f"{env}: --compare-to '{args.compare_to}' not found in {db}; skipping that section.")

    return sections


def write_env_report(env: str, sections: list[ReportSection], args: argparse.Namespace) -> None:
    env_dir = Path(args.outdir) / env
    env_dir.mkdir(parents=True, exist_ok=True)
    # Clear this report's own artifacts from prior runs first, so a changed axis,
    # build set, or --compare-to can't leave orphan PNGs / a stale HTML behind to
    # confuse which files belong to the current report. Scoped to our own outputs.
    for old in (*env_dir.glob("*.png"), env_dir / "report.md", env_dir / "report.html"):
        old.unlink(missing_ok=True)
    title = f"cardano-node stats report - {env}"
    subtitle = f"Builds: {', '.join(s.title for s in sections)}"

    if args.format in ("md", "both"):
        images = [img for sec in sections for img in sec.images]
        total = len(images)
        for i, img in enumerate(images, start=1):
            print(f"[{env}] rendering PNG {i}/{total}: {img.png_name}", flush=True)
            render_png(img.fig, env_dir / img.png_name, scale=args.scale)
        (env_dir / "report.md").write_text(assemble_markdown(title, sections, subtitle=subtitle))
        print(f"[{env}] wrote {env_dir / 'report.md'} (+ {total} PNGs)", flush=True)

    if args.format in ("html", "both"):
        print(f"[{env}] writing interactive HTML…", flush=True)
        out = env_dir / "report.html"
        out.write_text(assemble_html(title, sections, subtitle=subtitle, max_points=args.html_max_points))
        print(f"[{env}] wrote {out} ({out.stat().st_size / 1024 / 1024:.1f} MiB)", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--env",
        required=True,
        help="Environment(s): a name, comma-separated names, or 'all' "
        f"(of {', '.join(ENVS)}). Selects data/cardano-node/<env>.db.",
    )
    p.add_argument("--inmemory", default=None, help="Version token of the InMemory build (e.g. 11.0.1).")
    p.add_argument("--lsm", default=None, help="Version token of the LSM build (e.g. LSM-11.0.1).")
    p.add_argument(
        "--compare-to",
        default=None,
        help="Optional previous-version token to add a this-vs-previous comparison section.",
    )
    p.add_argument(
        "--format",
        choices=["md", "html", "both"],
        default="both",
        help="md (PNGs + Markdown, needs kaleido), html (interactive, no extra deps), or both.",
    )
    p.add_argument("--outdir", default=str(DEFAULT_OUT_DIR), help="Output root; per-env subdirs are created under it.")
    p.add_argument("--sqlite-db", default=None, help="Override the SQLite path (only valid with a single --env).")
    p.add_argument("--scale", type=int, default=2, help="PNG resolution scale (default 2).")
    p.add_argument(
        "--html-max-points",
        type=int,
        default=None,
        metavar="N",
        help="Downsample each HTML trace to at most N points to shrink the "
        "interactive file (PNGs stay full-resolution). Omit for full "
        "fidelity (default). ~4000 is a good balance - visually identical "
        "for these smooth curves and roughly halves the file. Lower N helps "
        "little past that: the remaining size is the panel count plus the "
        "inlined Plotly library, not the points. Stays a single offline "
        "self-contained HTML either way.",
    )
    args = p.parse_args()
    if not args.inmemory and not args.lsm:
        p.error("pass at least one of --inmemory / --lsm")
    return args


def main() -> None:
    args = parse_args()
    if args.env.strip() == "all":
        envs = list(ENVS)
    else:
        envs = [e.strip() for e in args.env.split(",") if e.strip()]
    if args.sqlite_db and len(envs) != 1:
        raise SystemExit("--sqlite-db is only valid with a single --env")

    try:
        for env in envs:
            db = args.sqlite_db or str(DEFAULT_DATA_DIR / f"{env}.db")
            if not Path(db).exists():
                warn(f"{env}: {db} not found; skipping.")
                continue
            sections = build_env_report(db, env, args)
            if sections:
                write_env_report(env, sections, args)
    except KaleidoMissingError as e:
        raise SystemExit(str(e)) from e


if __name__ == "__main__":
    main()
