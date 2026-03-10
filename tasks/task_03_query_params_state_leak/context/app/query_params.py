from __future__ import annotations


def build_query_params(page: int = 1, filters: dict[str, str] = {}) -> dict[str, str]:
    params = filters
    params.setdefault("page", str(page))
    return params
