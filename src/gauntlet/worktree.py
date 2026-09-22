from __future__ import annotations

from pathlib import Path

from .process import ProcessRunner


class WorktreeError(RuntimeError):
    pass


def primary_root(repo: Path, runner: ProcessRunner) -> Path:
    result = runner.run(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    if result.returncode:
        raise WorktreeError("gauntlet must be invoked from a Git working tree")
    return Path(result.stdout.strip()).resolve()


def worktree_path(repo_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        raise WorktreeError(f"invalid run ID: {run_id!r}")
    parent_dir = (repo_root.parent / f"{repo_root.name}.gauntlet-worktrees").resolve()
    path = (parent_dir / run_id).resolve()
    try:
        path.relative_to(parent_dir)
    except ValueError:
        raise WorktreeError(f"run ID path escapes worktrees directory: {run_id!r}")
    if repo_root == path or repo_root in path.parents:
        raise WorktreeError("run worktree must be outside the primary checkout")
    return path


def create_worktree(repo_root: Path, run_id: str, runner: ProcessRunner) -> Path:
    path = worktree_path(repo_root, run_id)
    if path.exists():
        raise WorktreeError(f"refusing to reuse existing worktree path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = runner.run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=repo_root)
    if result.returncode:
        raise WorktreeError(result.stderr.strip() or "git worktree add failed")
    if not path.is_dir() or repo_root in path.parents:
        raise WorktreeError("Git did not create an isolated worktree")
    return path
