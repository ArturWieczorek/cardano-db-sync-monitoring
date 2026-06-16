#!/usr/bin/env python3
"""Collector for the on-disk size of a cardano-node database directory (the
path passed to the node's ``--database-path``). Samples ``du`` of the whole dir
plus, separately, its optional ``lsm/`` subdir, and appends a time series to
data/cardano-node/<env>.db under the same version label node-resource-monitor.py uses.

See _disk_size.py for the shared mechanics and rationale. Pure collector - no
plotting, no prompts. Stops cleanly on SIGINT/SIGTERM with a peak/final summary.

Query the series directly, or (later) via node-plot.py --metrics disk:

    SELECT MAX(total_bytes) FROM disk_metrics WHERE version = '<label>';  -- peak
    SELECT total_bytes FROM disk_metrics WHERE version = '<label>'
      ORDER BY ts DESC LIMIT 1;                                           -- final
"""

import argparse
import sys
from pathlib import Path

from _disk_size import DiskSizeMonitor

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class NodeDbSizeMonitor(DiskSizeMonitor):
    DATA_DIR = PROJECT_ROOT / "data" / "cardano-node"
    BINARY_PREFIX = "cardano-node"
    PATH_FLAG = "--database-path"
    LABEL_PREFIX = "cardano-node"
    ENV_IN_ARGV = True  # cardano-node carries the env in --config/--socket-path/etc.


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="cardano-node db-directory size monitor (collector)")
    p.add_argument(
        "--env",
        required=True,
        choices=["mainnet", "preprod", "preview"],
        help="Environment name (selects data/cardano-node/<env>.db and the version label)",
    )
    p.add_argument(
        "--node-ver",
        required=True,
        help="Label tagging this run (e.g. LSM-11.0.1). Match the --node-ver you gave "
        "node-resource-monitor so the disk series joins the rest of the run.",
    )
    p.add_argument(
        "--path",
        default=None,
        help="Directory to measure (the node's --database-path). If omitted, it is "
        "auto-discovered from the running cardano-node's command line.",
    )
    p.add_argument(
        "--cardano-node-path",
        default="cardano-node",
        help="Executable name/path prefix of the cardano-node process (for argv discovery).",
    )
    p.add_argument(
        "--lsm-subdir",
        default="lsm",
        help="Name of the LSM subdir measured separately (default: lsm). "
        "Absent on stock/in-memory builds -> lsm_bytes is 0.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Sampling interval in seconds (default: 60 - du is heavy, keep it coarse).",
    )
    p.add_argument(
        "--du-timeout",
        type=float,
        default=120.0,
        help="Per-du timeout in seconds (default: 120). On timeout the sample is skipped.",
    )
    p.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit one JSON object per sample on stdout instead of the human form.",
    )
    p.add_argument(
        "--sqlite-db", default=None, help="Override the SQLite DB path (default: data/cardano-node/<env>.db)."
    )
    p.add_argument(
        "--match-arg",
        default=None,
        help="Extra substring that must appear in the matched node's command line, to "
        "disambiguate multiple nodes (e.g. LSM-mainnet). Used only for argv path "
        "discovery; ignored when --path is given.",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined,union-attr]
    args = parse_args()
    NodeDbSizeMonitor(
        env=args.env,
        version=args.node_ver,
        explicit_path=args.path,
        lsm_subdir=args.lsm_subdir,
        interval=args.interval,
        du_timeout=args.du_timeout,
        emit_json=args.emit_json,
        sqlite_db=args.sqlite_db,
        binary_prefix=args.cardano_node_path,
        match_arg=args.match_arg,
    ).run()
