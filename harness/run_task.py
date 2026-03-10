from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .apply_patch import (
    EditablePathError,
    ForbiddenEditError,
    PatchApplyError,
    PatchFormatError,
    apply_candidate_patch,
)
from .load_task import REPO_ROOT, TaskDefinition, load_task


@dataclass(frozen=True)
class CommandExecution:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskRunResult:
    task_id: str
    system: str
    patch_applied: bool
    baseline_failed: bool
    targeted_tests_passed: bool
    full_suite_passed: bool
    forbidden_edit: bool
    resolved: bool
    runtime_sec: float
    error_type: str | None
    patch_format: str | None
    changed_files: tuple[str, ...]
    result_path: str
    created_at: str
    baseline_command: str
    candidate_test_command: str
    full_test_command: str
    setup_command: str
    setup: CommandExecution | None
    baseline: CommandExecution | None
    targeted: CommandExecution | None
    full_suite: CommandExecution | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["changed_files"] = list(self.changed_files)
        return payload


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Task timed out before completing all steps")
    return remaining


def _run_command(command: str, cwd: Path, timeout_seconds: float) -> CommandExecution:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return CommandExecution(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_sec=round(time.monotonic() - started, 4),
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandExecution(
            command=command,
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            duration_sec=round(time.monotonic() - started, 4),
            timed_out=True,
        )


def _copy_task_workspace(task: TaskDefinition, workspace_root: Path) -> None:
    shutil.copytree(task.context_dir, workspace_root / "context")
    shutil.copytree(task.tests_dir, workspace_root / "tests")


def _write_result(results_dir: Path, system: str, task_id: str, payload: dict[str, Any]) -> Path:
    system_dir = results_dir / system
    system_dir.mkdir(parents=True, exist_ok=True)
    result_path = system_dir / f"{task_id}.json"
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result_path


def _finalize_result(
    *,
    task: TaskDefinition,
    system: str,
    results_dir: Path,
    patch_applied: bool,
    baseline_failed: bool,
    targeted_tests_passed: bool,
    full_suite_passed: bool,
    forbidden_edit: bool,
    runtime_sec: float,
    error_type: str | None,
    patch_format: str | None,
    changed_files: tuple[str, ...],
    setup: CommandExecution | None,
    baseline: CommandExecution | None,
    targeted: CommandExecution | None,
    full_suite: CommandExecution | None,
) -> TaskRunResult:
    resolved = (
        baseline_failed
        and patch_applied
        and not forbidden_edit
        and targeted_tests_passed
        and full_suite_passed
    )
    created_at = datetime.now(UTC).isoformat()
    provisional = {
        "task_id": task.task_id,
        "system": system,
        "patch_applied": patch_applied,
        "baseline_failed": baseline_failed,
        "targeted_tests_passed": targeted_tests_passed,
        "full_suite_passed": full_suite_passed,
        "forbidden_edit": forbidden_edit,
        "resolved": resolved,
        "runtime_sec": round(runtime_sec, 4),
        "error_type": error_type,
        "patch_format": patch_format,
        "changed_files": list(changed_files),
        "result_path": "",
        "created_at": created_at,
        "baseline_command": task.baseline_fail_command,
        "candidate_test_command": task.candidate_test_command,
        "full_test_command": task.full_test_command,
        "setup_command": task.setup_command,
        "setup": None if setup is None else setup.to_dict(),
        "baseline": None if baseline is None else baseline.to_dict(),
        "targeted": None if targeted is None else targeted.to_dict(),
        "full_suite": None if full_suite is None else full_suite.to_dict(),
    }
    result_path = _write_result(results_dir, system, task.task_id, provisional)

    result = TaskRunResult(
        task_id=task.task_id,
        system=system,
        patch_applied=patch_applied,
        baseline_failed=baseline_failed,
        targeted_tests_passed=targeted_tests_passed,
        full_suite_passed=full_suite_passed,
        forbidden_edit=forbidden_edit,
        resolved=resolved,
        runtime_sec=round(runtime_sec, 4),
        error_type=error_type,
        patch_format=patch_format,
        changed_files=changed_files,
        result_path=str(result_path),
        created_at=created_at,
        baseline_command=task.baseline_fail_command,
        candidate_test_command=task.candidate_test_command,
        full_test_command=task.full_test_command,
        setup_command=task.setup_command,
        setup=setup,
        baseline=baseline,
        targeted=targeted,
        full_suite=full_suite,
    )

    payload = result.to_dict()
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def run_task(
    *,
    task: TaskDefinition,
    system: str,
    patch_text: str,
    results_dir: Path,
    keep_workspace: bool = False,
) -> TaskRunResult:
    started = time.monotonic()
    deadline = started + task.timeout_seconds

    patch_applied = False
    baseline_failed = False
    targeted_tests_passed = False
    full_suite_passed = False
    forbidden_edit = False
    error_type: str | None = None
    patch_format: str | None = None
    changed_files: tuple[str, ...] = ()

    setup_result: CommandExecution | None = None
    baseline_result: CommandExecution | None = None
    targeted_result: CommandExecution | None = None
    full_suite_result: CommandExecution | None = None

    with tempfile.TemporaryDirectory(prefix=f"{task.task_id}_{system}_") as temp_dir:
        workspace_root = Path(temp_dir)
        _copy_task_workspace(task, workspace_root)

        if keep_workspace:
            debug_workspace = results_dir / "_workspaces" / system / task.task_id
            if debug_workspace.exists():
                shutil.rmtree(debug_workspace)
            debug_workspace.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workspace_root, debug_workspace)

        try:
            if task.setup_command:
                setup_result = _run_command(task.setup_command, workspace_root, _remaining_timeout(deadline))
                if setup_result.timed_out:
                    error_type = "timeout"
                    return _finalize_result(
                        task=task,
                        system=system,
                        results_dir=results_dir,
                        patch_applied=patch_applied,
                        baseline_failed=baseline_failed,
                        targeted_tests_passed=targeted_tests_passed,
                        full_suite_passed=full_suite_passed,
                        forbidden_edit=forbidden_edit,
                        runtime_sec=time.monotonic() - started,
                        error_type=error_type,
                        patch_format=patch_format,
                        changed_files=changed_files,
                        setup=setup_result,
                        baseline=baseline_result,
                        targeted=targeted_result,
                        full_suite=full_suite_result,
                    )
                if setup_result.exit_code != 0:
                    error_type = "setup_failed"
                    return _finalize_result(
                        task=task,
                        system=system,
                        results_dir=results_dir,
                        patch_applied=patch_applied,
                        baseline_failed=baseline_failed,
                        targeted_tests_passed=targeted_tests_passed,
                        full_suite_passed=full_suite_passed,
                        forbidden_edit=forbidden_edit,
                        runtime_sec=time.monotonic() - started,
                        error_type=error_type,
                        patch_format=patch_format,
                        changed_files=changed_files,
                        setup=setup_result,
                        baseline=baseline_result,
                        targeted=targeted_result,
                        full_suite=full_suite_result,
                    )

            baseline_result = _run_command(
                task.baseline_fail_command,
                workspace_root,
                _remaining_timeout(deadline),
            )
            if baseline_result.timed_out:
                error_type = "timeout"
                return _finalize_result(
                    task=task,
                    system=system,
                    results_dir=results_dir,
                    patch_applied=patch_applied,
                    baseline_failed=baseline_failed,
                    targeted_tests_passed=targeted_tests_passed,
                    full_suite_passed=full_suite_passed,
                    forbidden_edit=forbidden_edit,
                    runtime_sec=time.monotonic() - started,
                    error_type=error_type,
                    patch_format=patch_format,
                    changed_files=changed_files,
                    setup=setup_result,
                    baseline=baseline_result,
                    targeted=targeted_result,
                    full_suite=full_suite_result,
                )

            baseline_failed = baseline_result.exit_code != 0
            if not baseline_failed:
                error_type = "baseline_did_not_fail"
                return _finalize_result(
                    task=task,
                    system=system,
                    results_dir=results_dir,
                    patch_applied=patch_applied,
                    baseline_failed=baseline_failed,
                    targeted_tests_passed=targeted_tests_passed,
                    full_suite_passed=full_suite_passed,
                    forbidden_edit=forbidden_edit,
                    runtime_sec=time.monotonic() - started,
                    error_type=error_type,
                    patch_format=patch_format,
                    changed_files=changed_files,
                    setup=setup_result,
                    baseline=baseline_result,
                    targeted=targeted_result,
                    full_suite=full_suite_result,
                )

            patch_result = apply_candidate_patch(
                workspace_root=workspace_root,
                patch_text=patch_text,
                editable_paths=task.editable_paths,
                forbidden_paths=task.forbidden_paths,
                timeout_seconds=max(1, int(_remaining_timeout(deadline))),
            )
            patch_applied = True
            patch_format = patch_result.patch_format
            changed_files = patch_result.changed_files

            targeted_result = _run_command(
                task.candidate_test_command,
                workspace_root,
                _remaining_timeout(deadline),
            )
            if targeted_result.timed_out:
                error_type = "timeout"
                return _finalize_result(
                    task=task,
                    system=system,
                    results_dir=results_dir,
                    patch_applied=patch_applied,
                    baseline_failed=baseline_failed,
                    targeted_tests_passed=targeted_tests_passed,
                    full_suite_passed=full_suite_passed,
                    forbidden_edit=forbidden_edit,
                    runtime_sec=time.monotonic() - started,
                    error_type=error_type,
                    patch_format=patch_format,
                    changed_files=changed_files,
                    setup=setup_result,
                    baseline=baseline_result,
                    targeted=targeted_result,
                    full_suite=full_suite_result,
                )

            targeted_tests_passed = targeted_result.exit_code == 0
            if not targeted_tests_passed:
                error_type = "targeted_test_failed"
                return _finalize_result(
                    task=task,
                    system=system,
                    results_dir=results_dir,
                    patch_applied=patch_applied,
                    baseline_failed=baseline_failed,
                    targeted_tests_passed=targeted_tests_passed,
                    full_suite_passed=full_suite_passed,
                    forbidden_edit=forbidden_edit,
                    runtime_sec=time.monotonic() - started,
                    error_type=error_type,
                    patch_format=patch_format,
                    changed_files=changed_files,
                    setup=setup_result,
                    baseline=baseline_result,
                    targeted=targeted_result,
                    full_suite=full_suite_result,
                )

            full_suite_result = _run_command(
                task.full_test_command,
                workspace_root,
                _remaining_timeout(deadline),
            )
            if full_suite_result.timed_out:
                error_type = "timeout"
                return _finalize_result(
                    task=task,
                    system=system,
                    results_dir=results_dir,
                    patch_applied=patch_applied,
                    baseline_failed=baseline_failed,
                    targeted_tests_passed=targeted_tests_passed,
                    full_suite_passed=full_suite_passed,
                    forbidden_edit=forbidden_edit,
                    runtime_sec=time.monotonic() - started,
                    error_type=error_type,
                    patch_format=patch_format,
                    changed_files=changed_files,
                    setup=setup_result,
                    baseline=baseline_result,
                    targeted=targeted_result,
                    full_suite=full_suite_result,
                )

            full_suite_passed = full_suite_result.exit_code == 0
            if not full_suite_passed:
                error_type = "full_test_failed"

        except ForbiddenEditError:
            forbidden_edit = True
            error_type = "forbidden_edit"
        except EditablePathError:
            error_type = "non_editable_path"
        except PatchFormatError:
            error_type = "malformed_patch"
        except PatchApplyError:
            error_type = "patch_apply_failed"
        except TimeoutError:
            error_type = "timeout"

        return _finalize_result(
            task=task,
            system=system,
            results_dir=results_dir,
            patch_applied=patch_applied,
            baseline_failed=baseline_failed,
            targeted_tests_passed=targeted_tests_passed,
            full_suite_passed=full_suite_passed,
            forbidden_edit=forbidden_edit,
            runtime_sec=time.monotonic() - started,
            error_type=error_type,
            patch_format=patch_format,
            changed_files=changed_files,
            setup=setup_result,
            baseline=baseline_result,
            targeted=targeted_result,
            full_suite=full_suite_result,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one SWE-Bench Mini task in an isolated workspace.")
    parser.add_argument("--task-id", required=True, help="Task identifier to evaluate.")
    parser.add_argument("--system", required=True, help="System name to attribute the result to.")
    parser.add_argument("--patch", type=Path, required=True, help="Unified diff or overwrite patch file.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Benchmark repository root (defaults to the current repository).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "results",
        help="Directory where structured task results should be written.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Copy the pre-patch workspace into results/_workspaces for debugging.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    task = load_task(args.task_id, repo_root=args.repo_root)
    patch_text = args.patch.read_text(encoding="utf-8")
    result = run_task(
        task=task,
        system=args.system,
        patch_text=patch_text,
        results_dir=args.results_dir,
        keep_workspace=args.keep_workspace,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.resolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
