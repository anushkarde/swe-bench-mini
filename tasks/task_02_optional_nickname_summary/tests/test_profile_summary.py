from __future__ import annotations

from context.app.profile_summary import build_profile_summary


def test_optional_nickname_none_is_ignored() -> None:
    summary = build_profile_summary(
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "nickname": None,
            "email": "ADA@example.com",
        }
    )

    assert summary == "Ada Lovelace\nEmail: ada@example.com"


def test_nickname_is_trimmed_when_present() -> None:
    summary = build_profile_summary(
        {
            "first_name": "Grace",
            "last_name": "Hopper",
            "nickname": " Amazing Grace ",
            "email": "grace@example.com",
        }
    )

    assert summary == "Grace Hopper\nNickname: Amazing Grace\nEmail: grace@example.com"
