from __future__ import annotations

from context.app.query_params import build_query_params


def test_repeated_calls_do_not_share_state() -> None:
    assert build_query_params(page=2) == {"page": "2"}
    assert build_query_params() == {"page": "1"}


def test_existing_filters_are_preserved_without_mutating_input() -> None:
    filters = {"sort": "name"}

    params = build_query_params(filters=filters)

    assert params == {"sort": "name", "page": "1"}
    assert filters == {"sort": "name"}
