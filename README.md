# WikRev - Wiki Reviewer

A simple web app to review recent markdown changes in a git-based wiki with diffs, collapsing consecutive edits by the same person, previews, and optional AI summaries. Fast keyboard based navigation included!

Marvelous for periodic team overviews what's new in your knowledge base :)

> **Note:** This project was mostly AI-generated.

![WikRev Screenshot](docs/showcase.png)

## Motivation

Our team does weekly reviews of all wiki changes to share knowledge and stay aligned. Our wiki lives in the Azure DevOps wiki system, which stores content as a set of markdown files in a separate git repository. This tool was built to make those weekly review sessions easier—providing a clear view of what changed, who changed it, and why.

## Quick Start

```bash
uv sync
uv run wikrev
```

Open http://127.0.0.1:8010

Use `uv run wikrev` from this checkout when you want WikRev to use the repo's
`.venv`. A plain `wikrev` command may come from a separate `uv tool` install
with its own isolated environment.

If you change dependencies and usually launch WikRev via `uv tool`, refresh that
tool environment too, for example with `uv tool upgrade wikrev --reinstall` or
`uv tool install -e . --force`. Otherwise, run `uv run wikrev` from the repo
root.

## Features

- Shows markdown changes since last review
- Merges consecutive commits by same author on same file
- Displays raw diff, rendered diff, and final preview
- Renders Mermaid diagrams, including Azure DevOps `:::mermaid` blocks
- Links each change directly to its Azure DevOps wiki page when the URL can be inferred
- Optional AI summaries via GitHub Copilot SDK (needs Copilot CLI installed and authenticated)

## Config

Edit `.wikrev/config.json`:

| Key | Description |
|-----|-------------|
| `repo_path` | Path to the wiki, relative to the folder containing `.wikrev`. It may point at a subfolder of a larger git repo (e.g. `./Trouter`); changes outside that subfolder are ignored. |
| `wiki_base_url` | Optional wiki URL override. WikRev infers Azure DevOps URLs when possible; use `{path}` as an optional page-path placeholder. |
| `last_run` | ISO timestamp (or null for default) |
| `enable_copilot` | Enable AI summaries |
| `copilot_model` | Model used for AI summaries |
| `default_weekday` / `default_time` | Fallback review window when `last_run` is null |
| `path_filters` | Glob patterns (relative to `repo_path`) excluding pages from the review. Prefix a pattern with `!` to re-include, e.g. `["Release-Notes", "!Release-Notes/Template.md"]`. |
| `sort_order` | `newest_first` or `oldest_first` |

### Wiki in a subfolder

`repo_path` is the scope of the review. When it points at a subfolder, WikRev
resolves the folder's path relative to the git root and drops every changed file
outside it, so unrelated code, pipelines, and generated folders in the same
repository never show up (and never cost anything to process).

## Requirements

- Python 3.11+
- Git
