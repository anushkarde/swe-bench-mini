from __future__ import annotations


def expand_numeric_range(token: str) -> list[int]:
    token = token.strip()
    if not token:
        return []
    if "-" not in token:
        return [int(token)]

    start_text, end_text = token.split("-", 1)
    start = int(start_text)
    end = int(end_text)
    if end < start:
        raise ValueError("range end must be greater than or equal to start")

    return list(range(start, end))


def parse_number_list(spec: str) -> list[int]:
    values: list[int] = []
    for token in spec.split(","):
        values.extend(expand_numeric_range(token))
    return values
