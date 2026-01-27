from __future__ import annotations

import difflib
import html
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown import markdown

from .config import CONFIG_PATH, init_config, load_config, save_last_run, save_sort_order
from .git_changes import build_change_entries, get_change_details, get_commits_since, git_pull, group_consecutive, _get_repo_prefix
from .summarizer import get_cached_summary, set_cached_summary, summarize_with_copilot

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TEMPLATES.env.filters["urlencode"] = lambda value: quote(str(value), safe="")

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _extract_title(markdown_text: str | None, file_path: str) -> str:
    from urllib.parse import unquote
    return unquote(Path(file_path).name)


def _render_markdown(text: str | None) -> str:
    if not text:
        return ""
    return markdown(text, extensions=["tables", "fenced_code"])


def _render_inline_diff(base_text: str | None, head_text: str | None) -> str:
    """Render markdown with inline diff: deletions in red, additions in green.
    
    Strategy: render the full content, then use the raw text diff to mark
    which rendered blocks changed.
    """
    base = base_text or ""
    head = head_text or ""
    
    # Debug: log what we're getting
    import sys
    
    if not base and not head:
        return "<p><em>No content available.</em></p>"
    
    # If base is empty but head has content, it's a new file - show all as added
    if not base and head:
        rendered = markdown(head, extensions=["tables", "fenced_code"])
        return f'<div class="diff-added">{rendered}</div>'
    
    # If head is empty but base has content, file was deleted - show all as deleted
    if base and not head:
        rendered = markdown(base, extensions=["tables", "fenced_code"])
        return f'<div class="diff-deleted">{rendered}</div>'
    
    # Split into blocks (paragraphs/sections)
    base_lines = base.splitlines()
    head_lines = head.splitlines()
    
    result_parts: List[str] = []
    matcher = difflib.SequenceMatcher(None, base_lines, head_lines)
    
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # Unchanged lines - render as normal
            chunk = "\n".join(head_lines[j1:j2])
            if chunk.strip():
                result_parts.append(markdown(chunk, extensions=["tables", "fenced_code"]))
        elif tag == "delete":
            # Deleted lines - render with deletion styling
            chunk = "\n".join(base_lines[i1:i2])
            if chunk.strip():
                rendered = markdown(chunk, extensions=["tables", "fenced_code"])
                result_parts.append(f'<div class="diff-deleted">{rendered}</div>')
        elif tag == "insert":
            # Added lines - render with addition styling
            chunk = "\n".join(head_lines[j1:j2])
            if chunk.strip():
                rendered = markdown(chunk, extensions=["tables", "fenced_code"])
                result_parts.append(f'<div class="diff-added">{rendered}</div>')
        elif tag == "replace":
            # Changed lines - show both old and new
            old_chunk = "\n".join(base_lines[i1:i2])
            new_chunk = "\n".join(head_lines[j1:j2])
            if old_chunk.strip():
                rendered = markdown(old_chunk, extensions=["tables", "fenced_code"])
                result_parts.append(f'<div class="diff-deleted">{rendered}</div>')
            if new_chunk.strip():
                rendered = markdown(new_chunk, extensions=["tables", "fenced_code"])
                result_parts.append(f'<div class="diff-added">{rendered}</div>')
        
    return "".join(result_parts) if result_parts else "<p><em>No diff to show.</em></p>"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, weeks_back: int = 0):
    config = load_config()
    since = config.last_run - timedelta(weeks=weeks_back)
    repo_prefix = _get_repo_prefix(config.repo_path)
    
    commits = get_commits_since(config.repo_path, since)
    entries = build_change_entries(commits, config.excluded_folders, repo_prefix)
    groups = group_consecutive(entries)
    details = get_change_details(config.repo_path, groups)

    change_cards = []
    for detail in details:
        title = _extract_title(detail.head_content, detail.group.file_path)
        summary_key = detail.group.group_id
        summary = get_cached_summary(summary_key)
        change_cards.append(
            {
                "group": detail.group,
                "title": title,
                "diff_text": detail.diff_text,
                "split_diff_text": detail.split_diff_text,
                "rendered_diff": _render_inline_diff(detail.base_content, detail.head_content),
                "rendered_final": _render_markdown(detail.head_content),
                "summary": summary,
                "summary_key": summary_key,
            }
        )

    # Apply sort order
    if config.sort_order == "oldest_first":
        change_cards = list(reversed(change_cards))

    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "config_path": CONFIG_PATH,
            "last_run": since,
            "weeks_back": weeks_back,
            "changes": change_cards,
            "enable_copilot": config.enable_copilot,
            "sort_order": config.sort_order,
        },
    )


@app.post("/refresh")
async def refresh():
    config = load_config()
    git_pull(config.repo_path)
    return RedirectResponse(url="/", status_code=303)


@app.post("/clear-summaries")
async def clear_summaries():
    from .summarizer import CACHE_PATH
    if CACHE_PATH.exists():
        CACHE_PATH.unlink()
    return RedirectResponse(url="/", status_code=303)


@app.post("/mark-reviewed")
async def mark_reviewed():
    now = datetime.now().astimezone()
    save_last_run(now)
    return RedirectResponse(url="/", status_code=303)


@app.post("/toggle-sort-order")
async def toggle_sort_order():
    config = load_config()
    new_order = "oldest_first" if config.sort_order == "newest_first" else "newest_first"
    save_sort_order(new_order)
    return RedirectResponse(url="/", status_code=303)


@app.get("/summary/{summary_key:path}", response_class=HTMLResponse)
async def get_summary(request: Request, summary_key: str, weeks_back: int = 0):
    config = load_config()
    summary = get_cached_summary(summary_key)
    if summary:
        return HTMLResponse(html.escape(summary))

    if not config.enable_copilot:
        return HTMLResponse("Copilot summaries disabled in config.")

    since = config.last_run - timedelta(weeks=weeks_back)
    commits = get_commits_since(config.repo_path, since)
    repo_prefix = _get_repo_prefix(config.repo_path)
    entries = build_change_entries(commits, config.excluded_folders, repo_prefix)
    groups = group_consecutive(entries)
    detail_map = {g.group_id: g for g in groups}
    if summary_key not in detail_map:
        return HTMLResponse("Summary not available.")

    details = get_change_details(config.repo_path, [detail_map[summary_key]])
    diff_text = details[0].diff_text if details else ""

    try:
        summary = await summarize_with_copilot(diff_text, config.copilot_model)
    except Exception as exc:
        import logging
        import traceback
        logging.getLogger(__name__).error(
            "Failed to get Copilot summary for %s: %s\n%s",
            summary_key, exc, traceback.format_exc()
        )
        return HTMLResponse(html.escape(f"Error: {exc}"))

    set_cached_summary(summary_key, summary)
    return HTMLResponse(html.escape(summary))


def main() -> None:
    import argparse
    import sys

    import uvicorn

    parser = argparse.ArgumentParser(description="WikRev - Wiki Reviewer")
    parser.add_argument("--init", action="store_true", help="Initialize a config.json file in the current directory")
    args = parser.parse_args()

    if args.init:
        try:
            path = init_config()
            print(f"Created config file: {path}")
            print("Edit this file to configure your wiki repository path and other settings.")
        except FileExistsError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if not CONFIG_PATH.exists():
        print(f"Error: Config file not found: {CONFIG_PATH}", file=sys.stderr)
        print("Run 'wikrev --init' to create a default config file.", file=sys.stderr)
        sys.exit(1)

    uvicorn.run("wikrev.app:app", host="127.0.0.1", port=8010, reload=True)
