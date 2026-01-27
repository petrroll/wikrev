from __future__ import annotations

import fnmatch
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
    diff_text: str  # Merged diff (base -> head)
    split_diff_text: str  # Individual commit patches concatenated
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


def _should_exclude(file_path: str, path_filters: List[str], repo_prefix: str = "") -> bool:
    """Check if a file path should be excluded based on glob pattern filters.
    
    Args:
        file_path: Path from git (relative to git root)
        path_filters: Glob patterns to filter files. Prefix with ! to negate (include).
                     File-specific patterns override folder patterns.
        repo_prefix: Path prefix from git root to repo_path (e.g., 'Trouter/')
    
    Returns:
        True if the file should be excluded, False otherwise.
    
    Pattern matching rules:
        - Patterns without ! exclude matching files
        - Patterns with ! prefix include matching files (override exclusions)
        - More specific patterns (file-level) override less specific (folder-level)
        - Patterns are processed in order; later patterns can override earlier ones
    """
    if not path_filters:
        return False
    
    normalized = file_path.replace("\\", "/")
    # Strip the repo prefix to get path relative to repo_path
    if repo_prefix and normalized.startswith(repo_prefix):
        normalized = normalized[len(repo_prefix):]
    
    # Track exclusion state - None means no filter matched yet
    excluded = None
    
    for pattern in path_filters:
        is_negation = pattern.startswith("!")
        glob_pattern = pattern[1:] if is_negation else pattern
        
        # Check if pattern matches the file path
        # Support both direct match and directory prefix match
        matches = False
        
        # Try direct glob match
        if fnmatch.fnmatch(normalized, glob_pattern):
            matches = True
        # Try matching as directory prefix (e.g., "docs/*" or "docs/**")
        elif fnmatch.fnmatch(normalized, glob_pattern.rstrip("/") + "/*"):
            matches = True
        elif fnmatch.fnmatch(normalized, glob_pattern.rstrip("/") + "/**"):
            matches = True
        # Handle simple folder name without glob (backward compat)
        elif not any(c in glob_pattern for c in "*?["):
            folder_prefix = glob_pattern.rstrip("/") + "/"
            if normalized.startswith(folder_prefix) or normalized == glob_pattern.rstrip("/"):
                matches = True
        
        if matches:
            excluded = not is_negation
    
    return excluded if excluded is not None else False


def build_change_entries(commits: Iterable[CommitInfo], path_filters: Optional[List[str]] = None, repo_prefix: str = "") -> List[ChangeEntry]:
    entries: List[ChangeEntry] = []
    filters = path_filters or []
    for commit in commits:
        for file_path in commit.files:
            if not _is_markdown(file_path):
                continue
            if _should_exclude(file_path, filters, repo_prefix):
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
    """Group changes by author and file path.
    
    Changes to the same file by the same author are merged even if there are
    commits to other files in between.
    """
    groups: List[ChangeGroup] = []
    # Track groups by (author, file_path) to merge non-consecutive changes per file
    group_index: dict[tuple[str, str], ChangeGroup] = {}
    
    for entry in entries:
        key = (entry.author, entry.file_path)
        if key in group_index:
            # Merge with existing group for this author+file
            group = group_index[key]
            group.oldest_commit = entry.commit
            group.oldest_date = entry.date
            group.subjects.append(entry.subject)
            group.commits.append(entry.commit)
        else:
            # Create new group
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
            group_index[key] = group
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
    """Get file content at a specific git ref. Returns empty string if file doesn't exist."""
    try:
        return _run_git(["show", f"{ref}:{file_path}"], repo_path)
    except subprocess.CalledProcessError:
        return ""


def _diff_file(repo_path: Path, base_ref: str, head_ref: str, file_path: str) -> str:
    """Get unified diff between base and head refs for a file."""
    # file_path is relative to git root, so we need to run from git root
    try:
        git_root = _run_git(["rev-parse", "--show-toplevel"], repo_path).strip()
        cwd = Path(git_root)
    except subprocess.CalledProcessError:
        cwd = repo_path
    
    # Try standard diff first
    diff_text = _run_git(["diff", "--no-color", base_ref, head_ref, "--", file_path], cwd)
    if diff_text.strip():
        return diff_text
    
    # Fallback: get patch from the head commit directly (handles merge commits)
    return _run_git(["show", "--no-color", "--format=", "--patch", head_ref, "--", file_path], cwd)


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
    """Get the diff for a specific file from a specific commit."""
    full_diff = _run_git(["show", "-m", "--no-color", "--format=", "--patch", commit], repo_path)
    return _extract_file_diff(full_diff, file_path)


def get_change_details(repo_path: Path, groups: Iterable[ChangeGroup]) -> List[ChangeDetail]:
    details: List[ChangeDetail] = []
    for group in groups:
        base_ref = _get_parent_or_empty_tree(repo_path, group.oldest_commit)
        head_ref = group.newest_commit
        
        # Get base and head content
        base_content = _show_file(repo_path, base_ref, group.file_path)
        head_content = _show_file(repo_path, head_ref, group.file_path)
        
        # Merged diff (base -> head)
        merged_diff_text = _diff_file(repo_path, base_ref, head_ref, group.file_path)
        
        # Split diff (individual commit patches) - only compute if multiple commits
        if len(group.commits) > 1:
            patches = [_commit_patch(repo_path, c, group.file_path) for c in group.commits]
            split_diff_text = "\n".join(p for p in patches if p.strip())
        else:
            split_diff_text = merged_diff_text  # Same as merged for single commit
        
        details.append(ChangeDetail(
            group=group,
            diff_text=merged_diff_text,
            split_diff_text=split_diff_text,
            base_content=base_content,
            head_content=head_content
        ))
    return details
