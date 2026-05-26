"""Tests for Cardano era mapping (era_for, ERA_BY_PROTOCOL_MAJOR, era_sort_key)."""

import pytest
from _common import ERA_BY_PROTOCOL_MAJOR, ERA_ORDER, era_for, era_sort_key


class TestEraFor:
    """era_for maps protocol_major → era name."""

    @pytest.mark.parametrize(
        "proto,expected",
        [
            (0, "Byron"),
            (1, "Byron"),
            (2, "Shelley"),
            (3, "Allegra"),
            (4, "Mary"),
            (5, "Alonzo"),
            (6, "Alonzo"),
            (7, "Babbage"),
            (8, "Babbage"),
            (9, "Conway"),
            (10, "Conway"),
        ],
    )
    def test_known_versions(self, proto: int, expected: str):
        assert era_for(proto) == expected

    def test_none(self):
        assert era_for(None) == "Unknown"

    def test_unknown_future_proto_includes_number(self):
        # A future hard fork bumps to proto 11; era_for should clearly
        # surface that fact rather than silently bucketing as 'Unknown'.
        assert era_for(11) == "Unknown (proto 11)"

    def test_negative_proto(self):
        assert era_for(-1) == "Unknown (proto -1)"


class TestEraOrder:
    """ERA_ORDER is the canonical chronological order, derived from the dict."""

    def test_chronological_order(self):
        # Insertion order in ERA_BY_PROTOCOL_MAJOR is the source of truth.
        assert ERA_ORDER == ["Byron", "Shelley", "Allegra", "Mary", "Alonzo", "Babbage", "Conway"]

    def test_every_known_era_is_in_order(self):
        for proto, era in ERA_BY_PROTOCOL_MAJOR.items():
            assert era in ERA_ORDER, f"proto_major={proto} → era={era!r} not in ERA_ORDER"


class TestEraSortKey:
    """era_sort_key produces tuples that sort known eras chronologically,
    unknown eras alphabetically after them."""

    def test_known_eras_sort_chronologically(self):
        sorted_eras = sorted(["Conway", "Byron", "Mary", "Shelley"], key=era_sort_key)
        assert sorted_eras == ["Byron", "Shelley", "Mary", "Conway"]

    def test_unknown_sorts_after_all_known(self):
        sorted_eras = sorted(["Unknown (proto 11)", "Byron", "Conway"], key=era_sort_key)
        assert sorted_eras == ["Byron", "Conway", "Unknown (proto 11)"]

    def test_unknowns_relative_order(self):
        # Unknowns sort alphabetically among themselves.
        sorted_eras = sorted(
            ["Unknown (proto 12)", "Unknown (proto 11)", "Conway"], key=era_sort_key
        )
        assert sorted_eras == ["Conway", "Unknown (proto 11)", "Unknown (proto 12)"]
