from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class CommitInfo:
    commit: str
    author: str
    author_email: str
    date: datetime
    subject: str
    files: List[str]


@dataclass
class ChangeEntry:
    commit: str
    author: str
    date: datetime
    subject: str
    file_path: str


@dataclass
class ChangeGroup:
    group_id: str
    file_path: str
    author: str
    newest_commit: str
    oldest_commit: str
    newest_date: datetime
    oldest_date: datetime
    subjects: List[str] = field(default_factory=list)
    commits: List[str] = field(default_factory=list)


@dataclass
class ChangeDetail:
    group: ChangeGroup
    diff_text: str
    base_content: str
    head_content: str


def _run_git(args: List[str], cwd: Path, check: bool = True, input_text: Optional[str] = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
    )
    return result.stdout


def git_pull(repo_path: Path) -> str:
    return _run_git(["pull"], repo_path)


def _parse_log(output: str) -> List[CommitInfo]:
    commits: List[CommitInfo] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        if lines[i] != "==COMMIT==":
            i += 1
            continue
        if i + 5 >= len(lines):
            break
        commit = lines[i + 1].strip()
        author = lines[i + 2].strip()
        author_email = lines[i + 3].strip()
        date = datetime.fromisoformat(lines[i + 4].strip())
        subject = lines[i + 5].strip()
        i += 6
        files: List[str] = []
        while i < len(lines) and lines[i] != "==COMMIT==":
            if lines[i].strip():
                files.append(lines[i].strip())
            i += 1
        commits.append(CommitInfo(commit, author, author_email, date, subject, files))
    return commits


def _is_markdown(path: str) -> bool:
    return path.lower().endswith(".md")


def _get_repo_prefix(repo_path: Path) -> str:
    """Get the path prefix from git root to repo_path.
    
    If repo_path is a subfolder of the git repo, returns the relative path
    (e.g., 'Trouter/' if repo_path points to a Trouter subfolder).
    Returns empty string if repo_path is the git root.
    """
    try:
        git_root = _run_git(["rev-parse", "--show-toplevel"], repo_path).strip()
        git_root_path = Path(git_root).resolve()
        repo_resolved = repo_path.resolve()
        if repo_resolved != git_root_path:
            relative = repo_resolved.relative_to(git_root_path)
            return str(relative).replace("\\", "/") + "/"
    except (subprocess.CalledProcessError, ValueError):
        pass
    return ""


def get_commits_since(repo_path: Path, since: datetime) -> List[CommitInfo]:
    since_arg = since.astimezone().isoformat()
    output = _run_git(
        [
            "log",
            f"--since={since_arg}",
            "--name-only",
            "--date=iso-strict",
            "--pretty=format:==COMMIT==%n%H%n%an%n%ae%n%ad%n%s",
        ],
        repo_path,
    )
    return _parse_log(output)


def _is_excluded(file_path: str, excluded_folders: List[str], repo_prefix: str = "") -> bool:
    """Check if a file path is within any excluded folder.
    
    Args:
        file_path: Path from git (relative to git root)
        excluded_folders: Folders to exclude (relative to repo_path)
        repo_prefix: Path prefix from git root to repo_path (e.g., 'Trouter/')
    """
    normalized = file_path.replace("\\", "/")
    # Strip the repo prefix to get path relative to repo_path
    if repo_prefix and normalized.startswith(repo_prefix):
        normalized = normalized[len(repo_prefix):]
    for folder in excluded_folders:
        folder_prefix = folder.rstrip("/") + "/"
        if normalized.startswith(folder_prefix) or normalized == folder.rstrip("/"):
            return True
    return False


def build_change_entries(commits: Iterable[CommitInfo], excluded_folders: Optional[List[str]] = None, repo_prefix: str = "") -> List[ChangeEntry]:
    entries: List[ChangeEntry] = []
    excluded = excluded_folders or []
    for commit in commits:
        for file_path in commit.files:
            if not _is_markdown(file_path):
                continue
            if _is_excluded(file_path, excluded, repo_prefix):
                continue
            entries.append(
                ChangeEntry(
                    commit=commit.commit,
                    author=commit.author,
                    date=commit.date,
                    subject=commit.subject,
                    file_path=file_path,
                )
            )
    return entries


def group_consecutive(entries: List[ChangeEntry]) -> List[ChangeGroup]:
    groups: List[ChangeGroup] = []
    for entry in entries:
        if groups and groups[-1].author == entry.author and groups[-1].file_path == entry.file_path:
            group = groups[-1]
            group.oldest_commit = entry.commit
            group.oldest_date = entry.date
            group.subjects.append(entry.subject)
            group.commits.append(entry.commit)
        else:
            group = ChangeGroup(
                group_id=f"{entry.file_path}|{entry.commit}",
                file_path=entry.file_path,
                author=entry.author,
                newest_commit=entry.commit,
                oldest_commit=entry.commit,
                newest_date=entry.date,
                oldest_date=entry.date,
                subjects=[entry.subject],
                commits=[entry.commit],
            )
            groups.append(group)
    return groups


def _get_parent_or_empty_tree(repo_path: Path, commit: str) -> str:
    try:
        parent = _run_git(["rev-parse", f"{commit}^"], repo_path).strip()
        if parent:
            return parent
    except subprocess.CalledProcessError:
        pass
    empty_tree = _run_git(["hash-object", "-t", "tree", "--stdin"], repo_path, input_text="").strip()
    return empty_tree


def _show_file(repo_path: Path, ref: str, file_path: str) -> str:
    """Get file content at a specific git ref."""
    try:
        # Git show uses the path as stored in the tree, no need for shell escaping
        result = _run_git(["show", f"{ref}:{file_path}"], repo_path)
        return result
    except subprocess.CalledProcessError as e:
        # Log but don't fail - file might not exist at that ref
        import sys
        print(f"DEBUG: _show_file failed for {ref}:{file_path} - {e}", file=sys.stderr)
        return ""


def _diff_file(repo_path: Path, base_ref: str, head_ref: str, file_path: str) -> str:
    diff_text = _run_git(["diff", "--no-color", base_ref, head_ref, "--", file_path], repo_path)
    if diff_text.strip():
        return diff_text

    diff_text = _run_git(
        [
            "log",
            "--follow",
            "--no-color",
            "--format=",
            "--patch",
            f"{base_ref}..{head_ref}",
            "--",
            file_path,
        ],
        repo_path,
    )
    if diff_text.strip():
        return diff_text

    # Fallback: show latest commit patch for the file when range diff is empty.
    return _run_git(["show", "--no-color", "--format=", "--patch", head_ref, "--", file_path], repo_path)


def _extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the diff section for a specific file from a full commit diff."""
    lines = full_diff.splitlines(keepends=True)
    result: List[str] = []
    capturing = False
    # Normalize path for matching (handle both forward and back slashes)
    normalized_path = file_path.replace("\\", "/")
    
    for line in lines:
        if line.startswith("diff --git "):
            # Check if this diff block is for our file
            # Format: diff --git a/path/file b/path/file
            if normalized_path in line:
                capturing = True
                result.append(line)
            else:
                capturing = False
        elif capturing:
            result.append(line)
    
    return "".join(result)


def _commit_patch(repo_path: Path, commit: str, file_path: str) -> str:
    # Get the full commit diff and extract lines for this file
    full_diff = _run_git(["show", "-m", "--no-color", "--format=", "--patch", commit], repo_path)
    return _extract_file_diff(full_diff, file_path)


def _unified_diff_text(file_path: str, base_content: str, head_content: str) -> str:
    import difflib

    base_lines = base_content.splitlines(keepends=True)
    head_lines = head_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        base_lines,
        head_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    return "".join(diff)


def get_change_details(repo_path: Path, groups: Iterable[ChangeGroup]) -> List[ChangeDetail]:
    details: List[ChangeDetail] = []
    for group in groups:
        base_ref = _get_parent_or_empty_tree(repo_path, group.oldest_commit)
        head_ref = group.newest_commit
        patches: List[str] = []
        for commit in group.commits:
            patch = _commit_patch(repo_path, commit, group.file_path)
            if patch.strip():
                patches.append(patch)

        diff_text = "\n".join(patches).strip()
        if not diff_text:
            diff_text = _diff_file(repo_path, base_ref, head_ref, group.file_path)
        base_content = _show_file(repo_path, base_ref, group.file_path)
        head_content = _show_file(repo_path, head_ref, group.file_path)
        if not diff_text.strip() and (base_content or head_content):
            diff_text = _unified_diff_text(group.file_path, base_content, head_content)
        details.append(ChangeDetail(group=group, diff_text=diff_text, base_content=base_content, head_content=head_content))
    return details
