"""Tests for resolve_versions — the --versions CLI input → full-label mapping."""

import pytest
from _common import resolve_versions

AVAILABLE = [
    "cardano-db-sync 13.6.0.5 preprod",
    "cardano-db-sync 13.7.1.0 preprod",
    "cardano-db-sync 13.7.1.0-hasql preprod",
]


class TestResolveVersions:
    def test_exact_full_label(self):
        chosen = resolve_versions(["cardano-db-sync 13.6.0.5 preprod"], AVAILABLE)
        assert chosen == ["cardano-db-sync 13.6.0.5 preprod"]

    def test_short_token(self):
        chosen = resolve_versions(["13.6.0.5"], AVAILABLE)
        assert chosen == ["cardano-db-sync 13.6.0.5 preprod"]

    def test_multiple_short_tokens(self):
        chosen = resolve_versions(["13.6.0.5", "13.7.1.0-hasql"], AVAILABLE)
        assert chosen == [
            "cardano-db-sync 13.6.0.5 preprod",
            "cardano-db-sync 13.7.1.0-hasql preprod",
        ]

    def test_mixed_full_and_short(self):
        chosen = resolve_versions(
            ["cardano-db-sync 13.6.0.5 preprod", "13.7.1.0-hasql"], AVAILABLE
        )
        assert chosen == [
            "cardano-db-sync 13.6.0.5 preprod",
            "cardano-db-sync 13.7.1.0-hasql preprod",
        ]

    def test_missing_short_raises(self):
        with pytest.raises(SystemExit) as exc:
            resolve_versions(["does-not-exist"], AVAILABLE)
        assert "No version matches 'does-not-exist'" in str(exc.value)

    def test_missing_full_raises(self):
        with pytest.raises(SystemExit) as exc:
            resolve_versions(["cardano-db-sync 99.0 preprod"], AVAILABLE)
        assert "No version matches" in str(exc.value)

    def test_first_missing_aborts_before_remaining(self):
        # Order matters: the function raises on the first miss.
        with pytest.raises(SystemExit):
            resolve_versions(["bad", "13.6.0.5"], AVAILABLE)

    def test_empty_request_returns_empty(self):
        assert resolve_versions([], AVAILABLE) == []

    def test_ambiguous_short_token_raises(self):
        # If two available labels share a short token, the function rejects
        # ambiguous input rather than silently picking one.
        ambiguous = [
            "cardano-db-sync 13.6.0.5 preprod",
            "cardano-db-sync 13.6.0.5 preview",
        ]
        with pytest.raises(SystemExit) as exc:
            resolve_versions(["13.6.0.5"], ambiguous)
        assert "Ambiguous '13.6.0.5'" in str(exc.value)
