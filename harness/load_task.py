from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = REPO_ROOT / "tasks"
TASK_SCHEMA_PATH = TASKS_DIR / "task_schema.json"


class TaskNotFoundError(FileNotFoundError):
    """Raised when a requested task directory is missing."""


class TaskValidationError(ValueError):
    """Raised when task files do not conform to the benchmark contract."""


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    title: str
    difficulty: str
    tags: tuple[str, ...]
    task_dir: Path
    task_json_path: Path
    metadata_path: Path
    context_dir: Path
    tests_dir: Path
    gold_dir: Path
    entry_point: str | None
    editable_paths: tuple[str, ...]
    setup_command: str
    baseline_fail_command: str
    candidate_test_command: str
    full_test_command: str
    timeout_seconds: int
    forbidden_paths: tuple[str, ...]
    metadata: dict[str, Any]
    raw_task: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("task_dir", "task_json_path", "metadata_path", "context_dir", "tests_dir", "gold_dir"):
            payload[key] = str(payload[key])
        return payload


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise TaskValidationError(f"Missing required JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskValidationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskValidationError(f"Expected a JSON object in {path}")
    return payload


def load_task_schema(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    schema_path = repo_root / "tasks" / "task_schema.json"
    return _read_json(schema_path)


def _expect_non_empty_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TaskValidationError(f"`{key}` must be a string")
    if value == "":
        raise TaskValidationError(f"`{key}` must not be empty")
    return value


def _expect_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise TaskValidationError(f"`{key}` must be a string")
    return value


def _expect_string_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise TaskValidationError(f"`{key}` must be a non-empty array")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise TaskValidationError(f"`{key}` entries must be non-empty strings")
        items.append(item)
    return items


def _validate_task_payload(task_data: dict[str, Any], schema: dict[str, Any]) -> None:
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}).keys())
    allowed_difficulties = set(
        schema.get("properties", {}).get("difficulty", {}).get("enum", ["easy", "medium", "hard"])
    )

    missing = sorted(required - set(task_data))
    if missing:
        raise TaskValidationError(f"Missing required task fields: {', '.join(missing)}")

    unknown = sorted(set(task_data) - properties)
    if unknown:
        raise TaskValidationError(f"Unknown task fields: {', '.join(unknown)}")

    task_id = _expect_non_empty_string(task_data, "task_id")
    if task_id.startswith("task_") is False:
        raise TaskValidationError("`task_id` must start with `task_`")

    difficulty = _expect_non_empty_string(task_data, "difficulty")
    if difficulty not in allowed_difficulties:
        raise TaskValidationError(
            f"`difficulty` must be one of: {', '.join(sorted(allowed_difficulties))}"
        )

    _expect_non_empty_string(task_data, "title")
    _expect_string_list(task_data, "tags")
    _expect_string(task_data, "setup_command")
    _expect_non_empty_string(task_data, "baseline_fail_command")
    _expect_non_empty_string(task_data, "candidate_test_command")
    _expect_non_empty_string(task_data, "full_test_command")
    _expect_string_list(task_data, "forbidden_paths")

    timeout_seconds = task_data.get("timeout_seconds")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise TaskValidationError("`timeout_seconds` must be an integer >= 1")

    has_entry_point = "entry_point" in task_data
    has_editable_paths = "editable_paths" in task_data
    if not has_entry_point and not has_editable_paths:
        raise TaskValidationError("Task must define `entry_point` or `editable_paths`")
    if has_entry_point:
        _expect_non_empty_string(task_data, "entry_point")
    if has_editable_paths:
        _expect_string_list(task_data, "editable_paths")


def _validate_relative_patterns(values: list[str], field_name: str) -> None:
    for value in values:
        if value.startswith("/"):
            raise TaskValidationError(f"`{field_name}` must use relative paths, found absolute path: {value}")
        if ".." in Path(value).parts:
            raise TaskValidationError(f"`{field_name}` must not escape the workspace: {value}")


def _validate_task_layout(task_dir: Path, task_data: dict[str, Any]) -> None:
    if task_dir.name != task_data["task_id"]:
        raise TaskValidationError(
            f"Task directory name `{task_dir.name}` must match `task_id` `{task_data['task_id']}`"
        )

    required_paths = {
        "context": task_dir / "context",
        "tests": task_dir / "tests",
        "gold": task_dir / "gold",
        "metadata.json": task_dir / "metadata.json",
    }
    for label, path in required_paths.items():
        if not path.exists():
            raise TaskValidationError(f"Missing required task artifact `{label}` in {task_dir}")

    run_tests = task_dir / "tests" / "run_tests.sh"
    if not run_tests.exists():
        raise TaskValidationError(f"Missing required regression runner `{run_tests}`")

    editable_paths = list(task_data.get("editable_paths", []))
    if not editable_paths and "entry_point" in task_data:
        editable_paths = [task_data["entry_point"]]

    _validate_relative_patterns(editable_paths, "editable_paths")
    _validate_relative_patterns(list(task_data.get("forbidden_paths", [])), "forbidden_paths")


def discover_tasks(repo_root: Path = REPO_ROOT) -> list[Path]:
    tasks_dir = repo_root / "tasks"
    if not tasks_dir.exists():
        return []
    return sorted(
        path
        for path in tasks_dir.iterdir()
        if path.is_dir() and path.name.startswith("task_")
    )


def _build_task_definition(task_dir: Path, task_data: dict[str, Any], metadata: dict[str, Any]) -> TaskDefinition:
    entry_point = task_data.get("entry_point")
    editable_paths = list(task_data.get("editable_paths", []))
    if not editable_paths and entry_point is not None:
        editable_paths = [entry_point]

    return TaskDefinition(
        task_id=task_data["task_id"],
        title=task_data["title"],
        difficulty=task_data["difficulty"],
        tags=tuple(task_data["tags"]),
        task_dir=task_dir,
        task_json_path=task_dir / "task.json",
        metadata_path=task_dir / "metadata.json",
        context_dir=task_dir / "context",
        tests_dir=task_dir / "tests",
        gold_dir=task_dir / "gold",
        entry_point=entry_point,
        editable_paths=tuple(editable_paths),
        setup_command=task_data["setup_command"],
        baseline_fail_command=task_data["baseline_fail_command"],
        candidate_test_command=task_data["candidate_test_command"],
        full_test_command=task_data["full_test_command"],
        timeout_seconds=task_data["timeout_seconds"],
        forbidden_paths=tuple(task_data["forbidden_paths"]),
        metadata=metadata,
        raw_task=task_data,
    )


def load_task(task_id: str, repo_root: Path = REPO_ROOT) -> TaskDefinition:
    task_dir = repo_root / "tasks" / task_id
    if not task_dir.exists():
        raise TaskNotFoundError(f"Task directory not found: {task_dir}")

    schema = load_task_schema(repo_root)
    task_data = _read_json(task_dir / "task.json")
    metadata = _read_json(task_dir / "metadata.json")
    _validate_task_payload(task_data, schema)
    _validate_task_layout(task_dir, task_data)
    return _build_task_definition(task_dir, task_data, metadata)


def load_all_tasks(repo_root: Path = REPO_ROOT) -> list[TaskDefinition]:
    tasks: list[TaskDefinition] = []
    for task_dir in discover_tasks(repo_root):
        tasks.append(load_task(task_dir.name, repo_root=repo_root))
    return tasks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load and validate a SWE-Bench Mini task definition.")
    parser.add_argument("--task-id", help="Task identifier to load")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Benchmark repository root (defaults to the current repository).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered task directories instead of loading a single task.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list:
        task_ids = [path.name for path in discover_tasks(args.repo_root)]
        print(json.dumps({"tasks": task_ids}, indent=2))
        return 0

    if not args.task_id:
        parser.error("--task-id is required unless --list is used")

    task = load_task(args.task_id, repo_root=args.repo_root)
    print(json.dumps(task.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
