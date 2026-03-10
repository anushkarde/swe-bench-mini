from __future__ import annotations


def build_profile_summary(profile: dict[str, str | None]) -> str:
    first_name = profile["first_name"].strip()
    last_name = profile["last_name"].strip()
    nickname = profile.get("nickname", "").strip()
    email = profile["email"].strip().lower()

    lines = [f"{first_name} {last_name}"]
    if nickname:
        lines.append(f"Nickname: {nickname}")
    lines.append(f"Email: {email}")
    return "\n".join(lines)
