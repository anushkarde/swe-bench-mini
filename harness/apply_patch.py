from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


class PatchFormatError(ValueError):
    """Raised when the patch payload uses an unsupported format."""


class EditablePathError(ValueError):
    """Raised when the patch modifies files outside the allowed edit surface."""


class ForbiddenEditError(ValueError):
    """Raised when the patch attempts to touch immutable files."""


class PatchApplyError(RuntimeError):
    """Raised when the underlying patch tool fails to apply a patch."""


@dataclass(frozen=True)
class PatchApplication:
    patch_format: str
    changed_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_relative_path(raw_path: str) -> str:
    path = raw_path.strip().replace("\\", "/")
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    if path.startswith("/"):
        raise PatchFormatError(f"Patch path must be relative, got: {raw_path}")

    parts = []
    for part in PurePosixPath(path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PatchFormatError(f"Patch path must not escape the workspace: {raw_path}")
        parts.append(part)

    normalized = "/".join(parts)
    if normalized == "":
        raise PatchFormatError(f"Invalid empty patch path: {raw_path}")
    return normalized


def _path_matches(path: str, pattern: str) -> bool:
    normalized_path = PurePosixPath(path)
    normalized_pattern = pattern.rstrip("/")
    if normalized_path.match(normalized_pattern):
        return True
    if path == normalized_pattern:
        return True
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3]
        return path == prefix or path.startswith(f"{prefix}/")
    return False


def _ensure_paths_allowed(
    changed_files: Iterable[str],
    editable_paths: Sequence[str],
    forbidden_paths: Sequence[str],
) -> None:
    editable_patterns = list(editable_paths)
    forbidden_patterns = list(forbidden_paths)

    for path in changed_files:
        if any(_path_matches(path, pattern) for pattern in forbidden_patterns):
            raise ForbiddenEditError(f"Patch modifies forbidden path: {path}")
        if editable_patterns and not any(_path_matches(path, pattern) for pattern in editable_patterns):
            raise EditablePathError(f"Patch modifies path outside editable surface: {path}")


def _extract_changed_files_from_diff(patch_text: str) -> tuple[str, ...]:
    changed_files: list[str] = []
    seen: set[str] = set()

    diff_header = re.compile(r"^diff --git a/(.+) b/(.+)$")
    for line in patch_text.splitlines():
        match = diff_header.match(line)
        if match:
            candidate = _normalize_relative_path(match.group(2))
            if candidate not in seen:
                changed_files.append(candidate)
                seen.add(candidate)
            continue

        if line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0].strip()
            if path == "/dev/null":
                continue
            candidate = _normalize_relative_path(path)
            if candidate not in seen:
                changed_files.append(candidate)
                seen.add(candidate)

    if not changed_files:
        raise PatchFormatError("Unified diff does not declare any changed files")

    return tuple(changed_files)


def _run_git_apply(workspace_root: Path, patch_text: str, timeout_seconds: int | None) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as handle:
        handle.write(patch_text)
        patch_file = Path(handle.name)

    try:
        check_cmd = ["git", "apply", "--check", "--recount", str(patch_file)]
        apply_cmd = ["git", "apply", "--recount", str(patch_file)]

        kwargs = {
            "cwd": workspace_root,
            "capture_output": True,
            "text": True,
            "timeout": timeout_seconds,
            "check": False,
        }
        check_result = subprocess.run(check_cmd, **kwargs)
        if check_result.returncode != 0:
            message = (check_result.stderr or check_result.stdout).strip() or "git apply --check failed"
            raise PatchApplyError(message)

        apply_result = subprocess.run(apply_cmd, **kwargs)
        if apply_result.returncode != 0:
            message = (apply_result.stderr or apply_result.stdout).strip() or "git apply failed"
            raise PatchApplyError(message)
    except subprocess.TimeoutExpired as exc:
        raise PatchApplyError(f"Patch application timed out after {timeout_seconds} seconds") from exc
    finally:
        patch_file.unlink(missing_ok=True)


def _parse_overwrite_patch(patch_text: str) -> dict[str, str | None]:
    try:
        payload = json.loads(patch_text)
    except json.JSONDecodeError as exc:
        raise PatchFormatError(f"Invalid JSON overwrite patch: {exc}") from exc

    files = payload.get("files")
    if isinstance(files, dict):
        normalized: dict[str, str | None] = {}
        for path, content in files.items():
            if not isinstance(path, str):
                raise PatchFormatError("Overwrite patch file keys must be strings")
            if content is not None and not isinstance(content, str):
                raise PatchFormatError("Overwrite patch contents must be strings or null")
            normalized[_normalize_relative_path(path)] = content
        if not normalized:
            raise PatchFormatError("Overwrite patch must contain at least one file")
        return normalized

    if isinstance(files, list):
        normalized = {}
        for entry in files:
            if not isinstance(entry, dict):
                raise PatchFormatError("Overwrite patch file list entries must be objects")
            path = entry.get("path")
            content = entry.get("content")
            if not isinstance(path, str):
                raise PatchFormatError("Overwrite patch entries must include a string `path`")
            if content is not None and not isinstance(content, str):
                raise PatchFormatError("Overwrite patch entry `content` must be a string or null")
            normalized[_normalize_relative_path(path)] = content
        if not normalized:
            raise PatchFormatError("Overwrite patch must contain at least one file")
        return normalized

    raise PatchFormatError("Overwrite patch must contain a top-level `files` object or array")


def _apply_overwrite_patch(workspace_root: Path, patch_text: str) -> tuple[str, ...]:
    file_map = _parse_overwrite_patch(patch_text)
    for relative_path, content in file_map.items():
        target = workspace_root / relative_path
        if content is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tuple(file_map.keys())


def apply_candidate_patch(
    workspace_root: Path,
    patch_text: str,
    editable_paths: Sequence[str],
    forbidden_paths: Sequence[str],
    timeout_seconds: int | None = None,
) -> PatchApplication:
    if not patch_text.strip():
        raise PatchFormatError("Patch payload is empty")

    stripped = patch_text.lstrip()
    if stripped.startswith("{"):
        changed_files = _parse_overwrite_patch(patch_text).keys()
        _ensure_paths_allowed(changed_files, editable_paths, forbidden_paths)
        applied_files = _apply_overwrite_patch(workspace_root, patch_text)
        return PatchApplication(patch_format="overwrite_json", changed_files=applied_files)

    changed_files = _extract_changed_files_from_diff(patch_text)
    _ensure_paths_allowed(changed_files, editable_paths, forbidden_paths)
    _run_git_apply(workspace_root, patch_text, timeout_seconds=timeout_seconds)
    return PatchApplication(patch_format="unified_diff", changed_files=changed_files)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply a candidate patch to a copied task workspace.")
    parser.add_argument("--workspace", type=Path, required=True, help="Workspace root to patch.")
    parser.add_argument("--patch", type=Path, required=True, help="Path to a unified diff or overwrite patch.")
    parser.add_argument(
        "--editable-path",
        action="append",
        default=[],
        help="Editable path pattern. May be passed multiple times.",
    )
    parser.add_argument(
        "--forbidden-path",
        action="append",
        default=[],
        help="Forbidden path pattern. May be passed multiple times.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=None,
        help="Optional timeout for applying the patch.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    patch_text = args.patch.read_text(encoding="utf-8")
    result = apply_candidate_patch(
        workspace_root=args.workspace,
        patch_text=patch_text,
        editable_paths=args.editable_path,
        forbidden_paths=args.forbidden_path,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
