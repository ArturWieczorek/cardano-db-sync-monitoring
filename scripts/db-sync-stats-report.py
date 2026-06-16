#!/usr/bin/env python3
"""Auto-generate the per-environment cardano-db-sync stats report (the
LSM-vs-InMemory comparison documented in db-sync-report-template.md).

For each environment it reads that env's SQLite stats DB
(data/cardano-db-sync/<env>.db) and, for each build present, assembles the
CPU & RAM, Ingest Metrics, and Table Row Counts plots on both slot and time
axes - the figures db-sync-plot.py already builds - then a comparison section.
Two renderings are produced (see --format): a Markdown file with auto-rendered
PNGs (no more clicking Plotly's camera button) and a self-contained interactive
HTML. SQLite-read-only; never touches Postgres or the stats DB.

This is distinct from db-sync-epoch-report.py, which is a Postgres-connected
per-epoch/size/summary report.

Examples:
    # One env, both builds, both output formats (default):
    python scripts/db-sync-stats-report.py --env preprod \\
        --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1

    # All envs, add a previous-version comparison, HTML only (no kaleido needed):
    python scripts/db-sync-stats-report.py --env all --format html \\
        --inmemory 13.7.1.0-node-11.0.1 --lsm LSM-13.7.1.0-node-11.0.1 \\
        --compare-to 13.6.0.5-genesis
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from _common import VERSION_KEYED_TABLES, load_all_versions, resolve_versions, short, warn
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
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "cardano-db-sync"
DEFAULT_OUT_DIR = PROJECT_ROOT / "reports" / "cardano-db-sync"
ENVS = ("mainnet", "preprod", "preview")

# (kind, caption, filename infix). Order = report order. Comparison sections use
# only the headline metrics (resource + throughput).
METRICS = [
    ("cpu_ram", "CPU & RAM (RSS)", ""),
    ("ingest", "Ingest Metrics", "ingest-metrics-"),
    ("tables", "Tables Row Count", "tables-row-count-"),
]
HEADLINE = [("cpu_ram", "CPU & RAM (RSS)", ""), ("ingest", "Ingest Metrics", "ingest-metrics-")]
AXES = ("slot", "time")


def _load_dbplot():
    """Import the hyphenated sibling db-sync-plot.py as a module."""
    spec = importlib.util.spec_from_file_location("db_sync_plot", Path(__file__).with_name("db-sync-plot.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["db_sync_plot"] = mod  # so any future-annotation dataclasses resolve
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


dbplot = _load_dbplot()


def build_fig(db: str, versions: list[str], env: str, kind: str, x_axis: str) -> Figure | None:
    """Load + build one metric figure, or None if the data isn't plottable on
    this axis (e.g. a pre-ts DB on the time axis) - reported, never fatal."""
    try:
        if kind == "cpu_ram":
            mem_df, cpu_df = dbplot.load_cpu_ram(db, versions, x_axis)
            if mem_df.empty and cpu_df.empty:
                return None
            return dbplot.build_cpu_ram(mem_df, cpu_df, versions, env, x_axis)
        if kind == "ingest":
            df = dbplot.load_ingest(db, versions, x_axis)
            return None if df.empty else dbplot.build_ingest(df, versions, env, x_axis)
        if kind == "tables":
            df = dbplot.load_rowcounts(db, versions, x_axis)
            return None if df.empty else dbplot.build_rowcounts(df, versions, env, x_axis)
    except SystemExit as e:  # load_cpu_ram raises this for a pre-ts DB on time axis
        warn(f"skipped {kind} ({x_axis}-axis): {e}")
    except Exception as e:
        warn(f"skipped {kind} ({x_axis}-axis): {e}")
    return None


def _png_name(env: str, tag: str, infix: str, x_axis: str) -> str:
    return f"cardano-db-sync-{env}-{tag}-{infix}{x_axis}-axis.png"


def build_build_section(db: str, env: str, label: str, role: str) -> ReportSection:
    """One build's section: every metric on both axes."""
    tag = short(label)
    sec = ReportSection(title=f"{role} version ({tag})")
    for kind, caption, infix in METRICS:
        for axis in AXES:
            # Building a figure (large mainnet tables especially) can take a
            # while, so announce each one - otherwise the run looks hung here.
            print(f"[{env}] building {role} ({tag}): {caption} - {axis}-axis...", flush=True)
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

    Always uses the **slot** axis: the runs happened at different wall-clock
    times, so a time axis would shift the curves apart and make the comparison
    meaningless. Slot number is the shared chain position, so it aligns the runs.
    """
    sec = ReportSection(title=title)
    for kind, caption, infix in HEADLINE:
        print(f"[{env}] building {title}: {caption} - slot-axis...", flush=True)
        fig = build_fig(db, labels, env, kind, "slot")
        if fig is None:
            print(f"[{env}]   -> no data for this metric/axis, skipped", flush=True)
            continue
        sec.images.append(
            ReportImage(
                caption=f"{caption} - slot-axis",
                png_name=_png_name(env, tag, infix, "slot"),
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
    available = load_all_versions(db, list(VERSION_KEYED_TABLES["db-sync"]))
    if not available:
        warn(f"{env}: no versions found in {db}; skipping.")
        return None

    inmem = _resolve(args.inmemory, available)
    lsm = _resolve(args.lsm, available)
    if not inmem and not lsm:
        warn(f"{env}: neither --inmemory nor --lsm resolved against {db}; skipping.")
        return None

    print(f"[{env}] loading data and building figures...", flush=True)
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
    title = f"cardano-db-sync stats report - {env}"
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
        print(f"[{env}] writing interactive HTML...", flush=True)
        out = env_dir / "report.html"
        out.write_text(assemble_html(title, sections, subtitle=subtitle, max_points=args.html_max_points))
        print(f"[{env}] wrote {out} ({out.stat().st_size / 1024 / 1024:.1f} MiB)", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--env",
        required=True,
        help="Environment(s): a name, comma-separated names, or 'all' "
        f"(of {', '.join(ENVS)}). Selects data/cardano-db-sync/<env>.db.",
    )
    p.add_argument("--inmemory", default=None, help="Version token of the InMemory build (e.g. 13.7.1.0-node-11.0.1).")
    p.add_argument("--lsm", default=None, help="Version token of the LSM build (e.g. LSM-13.7.1.0-node-11.0.1).")
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
        "for these smooth curves and roughly halves the file (e.g. a "
        "multi-day mainnet report.html ~94->~25 MiB, preprod ~20->~11 MiB). "
        "Lower N helps little past that: the remaining size is the panel "
        "count plus the inlined Plotly library, not the points. Stays a "
        "single offline self-contained HTML either way.",
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
