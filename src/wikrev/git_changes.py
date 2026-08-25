from __future__ import annotations

import fnmatch
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar
from urllib.parse import quote, urlparse

# Guard rails so a misbehaving git invocation can never hang the web server.
GIT_TIMEOUT_SECONDS = 120.0
GIT_PULL_TIMEOUT_SECONDS = 60.0
# Independent read-only git commands are spawned in parallel; process startup
# dominates their cost, so a handful of workers is a large win.
GIT_MAX_WORKERS = 8

_T = TypeVar("_T")
_K = TypeVar("_K")


def _map_parallel(func: Callable[[_K], _T], keys: Sequence[_K]) -> Dict[_K, _T]:
    """Run ``func`` over ``keys``, in parallel when it is worth the thread overhead."""
    if not keys:
        return {}
    if len(keys) == 1:
        return {keys[0]: func(keys[0])}
    with ThreadPoolExecutor(max_workers=min(GIT_MAX_WORKERS, len(keys))) as pool:
        return dict(zip(keys, pool.map(func, keys)))


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


def _git_env() -> Dict[str, str]:
    """Environment that stops git from blocking on interactive prompts."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def _run_git(
    args: List[str],
    cwd: Path,
    check: bool = True,
    input_text: Optional[str] = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args[:2])} timed out after {timeout}s in {cwd}") from exc
    return result.stdout


def _run_git_bytes(
    args: List[str],
    cwd: Path,
    input_bytes: Optional[bytes] = None,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bytes:
    """Run git without text decoding, for commands that emit raw object data."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            input=input_bytes,
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args[:2])} timed out after {timeout}s in {cwd}") from exc
    return result.stdout


def git_pull(repo_path: Path) -> str:
    return _run_git(["pull"], repo_path, timeout=GIT_PULL_TIMEOUT_SECONDS)


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


@lru_cache(maxsize=64)
def _git_root_cached(repo_path_str: str) -> Optional[str]:
    try:
        root = _run_git(["rev-parse", "--show-toplevel"], Path(repo_path_str), timeout=30).strip()
    except (subprocess.CalledProcessError, RuntimeError, OSError):
        return None
    return root or None


def _git_root(repo_path: Path) -> Path:
    """Git working-tree root for ``repo_path`` (cached; falls back to repo_path)."""
    root = _git_root_cached(str(repo_path.resolve()))
    return Path(root) if root else repo_path


def _get_repo_prefix(repo_path: Path) -> str:
    """Get the path prefix from git root to repo_path.

    If repo_path is a subfolder of the git repo, returns the relative path
    (e.g., 'Trouter/' if repo_path points to a Trouter subfolder).
    Returns empty string if repo_path is the git root.
    """
    try:
        git_root_path = _git_root(repo_path).resolve()
        repo_resolved = repo_path.resolve()
        if repo_resolved != git_root_path:
            relative = repo_resolved.relative_to(git_root_path)
            return str(relative).replace("\\", "/") + "/"
    except (OSError, ValueError):
        pass
    return ""


def infer_azure_wiki_base_url(repo_path: Path) -> Optional[str]:
    """Infer an Azure DevOps wiki URL from the origin, repo path, and branch."""
    try:
        remote_url = _run_git(["remote", "get-url", "origin"], repo_path).strip()
        parsed = urlparse(remote_url)
        parts = [part for part in parsed.path.split("/") if part]
        git_index = parts.index("_git")
        if parsed.hostname != "dev.azure.com" or git_index < 2:
            return None

        organization = parts[0]
        project = parts[1]
        repository = parts[git_index + 1]
        repo_prefix = _get_repo_prefix(repo_path)
        wiki_name = repo_path.name if repo_prefix else repository.removesuffix(".wiki")
        branch = _run_git(["branch", "--show-current"], repo_path).strip()

        base_url = (
            f"{parsed.scheme}://{parsed.hostname}/"
            f"{quote(organization, safe='')}/{quote(project, safe='')}/"
            f"_wiki/wikis/{quote(wiki_name, safe='')}"
        )
        return f"{base_url}?wikiVersion=GB{quote(branch, safe='')}" if branch else base_url
    except (subprocess.CalledProcessError, RuntimeError, ValueError, IndexError):
        return None


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
            normalized = file_path.replace("\\", "/")
            # `git log` reports the whole repository; keep only what lives under repo_path.
            if repo_prefix and not normalized.startswith(repo_prefix):
                continue
            if not _is_markdown(normalized):
                continue
            if _should_exclude(normalized, filters, repo_prefix):
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


EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@lru_cache(maxsize=8)
def _empty_tree_sha(repo_path_str: str) -> str:
    try:
        sha = _run_git(
            ["hash-object", "-t", "tree", "--stdin"], Path(repo_path_str), input_text="", timeout=30
        ).strip()
    except (subprocess.CalledProcessError, RuntimeError, OSError):
        return EMPTY_TREE_SHA
    return sha or EMPTY_TREE_SHA


def _batch_resolve(cwd: Path, revs: Sequence[str]) -> Dict[str, Optional[str]]:
    """Resolve many revisions with a single ``git cat-file --batch-check`` call."""
    unique = [rev for rev in dict.fromkeys(revs) if rev and "\n" not in rev]
    if not unique:
        return {}
    stdout = _run_git_bytes(
        ["cat-file", "--batch-check"],
        cwd,
        input_bytes=("\n".join(unique) + "\n").encode("utf-8"),
    )
    resolved: Dict[str, Optional[str]] = {}
    lines = stdout.decode("utf-8", "replace").splitlines()
    for rev, line in zip(unique, lines):
        parts = line.rsplit(" ", 2)
        if len(parts) == 3 and parts[2].isdigit():
            resolved[rev] = parts[0]
        else:
            resolved[rev] = None
    return resolved


def _batch_show_files(cwd: Path, specs: Sequence[str]) -> Dict[str, str]:
    """Read many ``<rev>:<path>`` blobs with a single ``git cat-file --batch`` call."""
    unique = [spec for spec in dict.fromkeys(specs) if spec and "\n" not in spec]
    if not unique:
        return {}
    stdout = _run_git_bytes(
        ["cat-file", "--batch"],
        cwd,
        input_bytes=("\n".join(unique) + "\n").encode("utf-8"),
    )

    contents: Dict[str, str] = {}
    pos = 0
    for spec in unique:
        newline = stdout.find(b"\n", pos)
        if newline == -1:
            contents[spec] = ""
            continue
        header = stdout[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        parts = header.rsplit(" ", 2)
        if len(parts) != 3 or not parts[2].isdigit():
            # `<input> missing` / `<input> ambiguous` -- no payload follows.
            contents[spec] = ""
            continue
        size = int(parts[2])
        payload = stdout[pos:pos + size]
        pos += size + 1  # git appends a newline after each object payload
        contents[spec] = payload.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return contents


def _get_parent_or_empty_tree(repo_path: Path, commit: str) -> str:
    try:
        parent = _run_git(["rev-parse", f"{commit}^"], repo_path).strip()
        if parent:
            return parent
    except (subprocess.CalledProcessError, RuntimeError):
        pass
    return _empty_tree_sha(str(repo_path.resolve()))


def _show_file(repo_path: Path, ref: str, file_path: str) -> str:
    """Get file content at a specific git ref. Returns empty string if file doesn't exist."""
    spec = f"{ref}:{file_path}"
    return _batch_show_files(_git_root(repo_path), [spec]).get(spec, "")


def _diff_header_matches(line: str, normalized_path: str) -> bool:
    """Whether a ``diff --git`` header line refers to ``normalized_path``.

    Matches on the ``b/<path>`` side (optionally quoted by git) so that a path
    which is a suffix of another path cannot capture the wrong hunk.
    """
    stripped = line.rstrip("\r\n")
    return stripped.endswith(f" b/{normalized_path}") or stripped.endswith(f'b/{normalized_path}"')


def _extract_file_diff(full_diff: str, file_path: str) -> str:
    """Extract the diff section for a specific file from a full commit diff."""
    if not full_diff:
        return ""
    # Normalize path for matching (handle both forward and back slashes)
    normalized_path = file_path.replace("\\", "/")
    lines = full_diff.splitlines(keepends=True)

    def collect(matcher) -> str:
        result: List[str] = []
        capturing = False
        for line in lines:
            if line.startswith("diff --git "):
                # Format: diff --git a/path/file b/path/file
                capturing = matcher(line)
                if capturing:
                    result.append(line)
            elif capturing:
                result.append(line)
        return "".join(result)

    exact = collect(lambda line: _diff_header_matches(line, normalized_path))
    if exact:
        return exact
    # Fallback for unusual prefixes (e.g. diff.noprefix) or renamed paths.
    return collect(lambda line: normalized_path in line)


def _resolve_base_refs(repo_path: Path, cwd: Path, groups: Sequence[ChangeGroup]) -> List[str]:
    """Resolve every group's base ref (parent of its oldest commit) in one git call."""
    parent_revs = [f"{group.oldest_commit}^" for group in groups]
    try:
        resolved = _batch_resolve(cwd, parent_revs)
    except (RuntimeError, OSError):
        resolved = {}

    base_refs: List[str] = []
    for group, rev in zip(groups, parent_revs):
        parent = resolved.get(rev)
        if parent:
            base_refs.append(parent)
        else:
            base_refs.append(_get_parent_or_empty_tree(repo_path, group.oldest_commit))
    return base_refs


def _batch_pair_diffs(cwd: Path, groups: Sequence[ChangeGroup], base_refs: Sequence[str]) -> Dict[int, str]:
    """Merged base->head diff per group, batching one git call per (base, head) pair."""
    pair_files: Dict[Tuple[str, str], List[str]] = {}
    for group, base_ref in zip(groups, base_refs):
        pair_files.setdefault((base_ref, group.newest_commit), []).append(group.file_path)

    def diff_pair(pair: Tuple[str, str]) -> str:
        base_ref, head_ref = pair
        files = sorted(set(pair_files[pair]))
        return _run_git(["diff", "--no-color", base_ref, head_ref, "--", *files], cwd)

    pair_output = _map_parallel(diff_pair, list(pair_files))

    diffs: Dict[int, str] = {}
    missing: List[int] = []
    for index, (group, base_ref) in enumerate(zip(groups, base_refs)):
        text = _extract_file_diff(pair_output[(base_ref, group.newest_commit)], group.file_path)
        diffs[index] = text
        if not text.strip():
            missing.append(index)

    # Fallback: patch straight from the head commit (handles merge commits)
    def head_patch(index: int) -> str:
        group = groups[index]
        return _run_git(
            ["show", "--no-color", "--format=", "--patch", group.newest_commit, "--", group.file_path],
            cwd,
        )

    diffs.update(_map_parallel(head_patch, missing))
    return diffs


def _batch_commit_patches(cwd: Path, groups: Sequence[ChangeGroup]) -> Dict[int, str]:
    """Per-commit patches for multi-commit groups, batching one git call per commit."""
    files_by_commit: Dict[str, set] = {}
    for group in groups:
        if len(group.commits) <= 1:
            continue
        for commit in group.commits:
            files_by_commit.setdefault(commit, set()).add(group.file_path)

    def commit_patch(commit: str) -> str:
        files = sorted(files_by_commit[commit])
        return _run_git(
            ["show", "-m", "--no-color", "--format=", "--patch", commit, "--", *files], cwd
        )

    commit_patches = _map_parallel(commit_patch, list(files_by_commit))

    split_diffs: Dict[int, str] = {}
    for index, group in enumerate(groups):
        if len(group.commits) <= 1:
            continue
        patches = [
            _extract_file_diff(commit_patches.get(commit, ""), group.file_path)
            for commit in group.commits
        ]
        split_diffs[index] = "\n".join(p for p in patches if p.strip())
    return split_diffs


def get_change_details(repo_path: Path, groups: Iterable[ChangeGroup]) -> List[ChangeDetail]:
    group_list = list(groups)
    if not group_list:
        return []

    # All file paths are relative to the git root, so batch commands run from there.
    cwd = _git_root(repo_path)
    base_refs = _resolve_base_refs(repo_path, cwd, group_list)

    content_specs: List[str] = []
    for group, base_ref in zip(group_list, base_refs):
        content_specs.append(f"{base_ref}:{group.file_path}")
        content_specs.append(f"{group.newest_commit}:{group.file_path}")
    contents = _batch_show_files(cwd, content_specs)

    merged_diffs = _batch_pair_diffs(cwd, group_list, base_refs)
    split_diffs = _batch_commit_patches(cwd, group_list)

    details: List[ChangeDetail] = []
    for index, (group, base_ref) in enumerate(zip(group_list, base_refs)):
        merged_diff_text = merged_diffs.get(index, "")
        details.append(ChangeDetail(
            group=group,
            diff_text=merged_diff_text,
            # Single-commit groups have nothing to split apart.
            split_diff_text=split_diffs.get(index, merged_diff_text),
            base_content=contents.get(f"{base_ref}:{group.file_path}", ""),
            head_content=contents.get(f"{group.newest_commit}:{group.file_path}", ""),
        ))
    return details
