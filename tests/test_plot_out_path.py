"""Locks the filename scheme used by db-sync-plot and node-plot for their
output HTML files.

The scheme is ``<env>_<versions>_<kind>_by_<axis>.html``, applied identically
to both scripts. Previously the cpu_ram plot was treated as "the default"
and got no kind tag (so its filename was ambiguous when sitting alongside
ingest/tables files), the x-axis was encoded as a bare ``_time`` suffix
(which read as a timestamp at a glance) or no suffix at all for slot mode,
and the env was only in the parent directory — once a file was moved or
shared, its env context vanished. All three gaps surfaced in real use;
these tests pin the fix so they don't re-appear.

Tests use ``tmp_path`` for the outdir so the env subdir is created somewhere
disposable, then assert on the filename (the env-subdir prefix path is
incidental and verified separately).
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(script_name: str, module_alias: str):
    spec = importlib.util.spec_from_file_location(
        module_alias, SCRIPTS_DIR / script_name
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


db_sync_plot = _load("db-sync-plot.py", "db_sync_plot_module")
node_plot = _load("node-plot.py", "node_plot_module")


# Each plot script has its own out_path. Parametrize so any divergence shows
# up as the same test failing on one side and passing on the other — easier
# to diagnose than one combined-name test.
@pytest.fixture(params=[("db_sync", db_sync_plot), ("node", node_plot)],
                ids=["db-sync-plot", "node-plot"])
def out_path_fn(request: pytest.FixtureRequest):
    """Yield (label, callable) for each script's out_path."""
    return request.param


class TestOutPath:
    def test_single_version_cpu_ram_by_slot(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "slot",
        )
        assert Path(path).name == "preprod_13.7.1.0_cpu_ram_by_slot.html"

    def test_single_version_cpu_ram_by_time(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "time",
        )
        assert Path(path).name == "preprod_13.7.1.0_cpu_ram_by_time.html"

    def test_single_version_ingest_carries_kind_and_axis(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "ingest", "time",
        )
        assert Path(path).name == "preprod_13.7.1.0_ingest_by_time.html"

    def test_single_version_tables_carries_kind_and_axis(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "tables", "slot",
        )
        assert Path(path).name == "preprod_13.7.1.0_tables_by_slot.html"

    def test_two_versions_compose_with_vs_separator(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        """``<short_a>_vs_<short_b>`` is the comparison-mode marker; the
        kind/axis tags follow it, not the version pair."""
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.6.0.5 preprod",
             "cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "time",
        )
        assert Path(path).name == "preprod_13.6.0.5_vs_13.7.1.0_cpu_ram_by_time.html"

    def test_env_is_first_word_of_filename(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        """Env name leads the filename so the file remains identifiable when
        moved out of its parent dir (which also encodes the env). Same
        convention used by db-sync-report's outputs."""
        _label, mod = out_path_fn
        for env in ("mainnet", "preprod", "preview"):
            path = mod.out_path(
                str(tmp_path), env,
                ["cardano-db-sync 13.7.1.0 " + env],
                "cpu_ram", "time",
            )
            name = Path(path).name
            assert name.startswith(f"{env}_"), (
                f"filename must start with the env; got: {name!r}"
            )

    def test_two_versions_env_appears_once_not_per_version(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        """Env at the front of the filename; not duplicated per version on
        either side of `_vs_`. The SQLite stats DB is per-env so both
        versions are guaranteed to share it."""
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.6.0.5 preprod",
             "cardano-db-sync 13.7.1.0 preprod"],
            "ingest", "time",
        )
        name = Path(path).name
        # 'preprod' appears exactly once (at the front).
        assert name.count("preprod") == 1, (
            f"env must appear exactly once; got: {name!r}"
        )

    def test_env_subdir_is_created_under_outdir(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        """The env name becomes a subdirectory of outdir, and out_path
        eagerly creates it (so the plot write doesn't fail on a fresh
        outdir)."""
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preview",
            ["cardano-db-sync 13.7.1.0 preview"],
            "tables", "time",
        )
        assert (tmp_path / "preview").is_dir()
        assert Path(path).parent == tmp_path / "preview"

    def test_axis_only_two_values_slot_or_time(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        """No silent capture of unexpected axis values — they end up in the
        filename verbatim, which makes a typo discoverable in the output
        rather than silently merged with another file. (Not a guard, just a
        pinned behaviour: out_path doesn't validate.)"""
        _label, mod = out_path_fn
        slot_path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "slot",
        )
        time_path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "time",
        )
        assert slot_path != time_path, (
            "slot and time outputs must not collide — that's why the axis "
            "tag exists. Got: " + repr(slot_path)
        )


class TestNoLegacyCollision:
    """Defensive: make sure the new scheme doesn't accidentally produce a
    name that matches the OLD legacy scheme (`<vers>.html` for cpu_ram,
    bare `_time` suffix). If someone partially reverts the rename, this
    catches it."""

    def test_cpu_ram_filename_is_not_bare_versions(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "cpu_ram", "slot",
        )
        assert Path(path).name != "13.7.1.0.html"

    def test_time_suffix_is_not_bare(
        self, tmp_path: Path, out_path_fn,
    ) -> None:
        _label, mod = out_path_fn
        path = mod.out_path(
            str(tmp_path), "preprod",
            ["cardano-db-sync 13.7.1.0 preprod"],
            "ingest", "time",
        )
        # Old scheme would have produced "13.7.1.0_ingest_time.html"; new
        # scheme inserts "by" so the axis name reads naturally.
        assert "_by_time" in Path(path).name
        assert "_ingest_time.html" not in Path(path).name
