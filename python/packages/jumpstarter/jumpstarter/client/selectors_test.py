"""Tests for label selector matching."""

from jumpstarter.client.selectors import selector_contains


class TestSelectorContains:
    """Tests for checking if a lease's selector contains a filter's criteria."""

    def test_exact_match_labels(self):
        assert selector_contains("board=rpi", "board=rpi") is True

    def test_subset_match_labels(self):
        assert selector_contains("board=rpi,env=prod", "board=rpi") is True

    def test_no_match_labels(self):
        assert selector_contains("board=jetson", "board=rpi") is False

    def test_exact_match_expressions(self):
        assert selector_contains("firmware in (v2, v3)", "firmware in (v2, v3)") is True

    def test_match_mixed(self):
        lease = "board=rpi,firmware in (v2, v3)"
        assert selector_contains(lease, "board=rpi") is True
        assert selector_contains(lease, "firmware in (v2, v3)") is True
        assert selector_contains(lease, "board=rpi,firmware in (v2, v3)") is True

    def test_no_match_expression(self):
        assert selector_contains("board=rpi", "firmware in (v2, v3)") is False

    def test_filter_not_exists(self):
        assert selector_contains("board=rpi,!experimental", "!experimental") is True
        assert selector_contains("board=rpi", "!experimental") is False

    def test_empty_filter_matches_all(self):
        assert selector_contains("board=rpi,firmware in (v2, v3)", "") is True

    def test_whitespace_tolerance(self):
        """Whitespace around operators should be tolerated (matching Go behavior)."""
        assert selector_contains("board=rpi", "board = rpi") is True
        assert selector_contains("board=rpi", "board =rpi") is True
        assert selector_contains("board=rpi", "board= rpi") is True
        assert selector_contains("firmware!=v3", "firmware != v3") is True
