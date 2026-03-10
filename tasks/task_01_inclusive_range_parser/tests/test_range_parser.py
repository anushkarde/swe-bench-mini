from __future__ import annotations

import pytest

from context.app.range_parser import expand_numeric_range, parse_number_list


def test_inclusive_upper_bound_is_preserved() -> None:
    assert expand_numeric_range("2-4") == [2, 3, 4]


def test_parse_number_list_supports_mixed_tokens() -> None:
    assert parse_number_list("1, 3-5, 8") == [1, 3, 4, 5, 8]


def test_descending_ranges_raise_value_error() -> None:
    with pytest.raises(ValueError):
        expand_numeric_range("5-3")
