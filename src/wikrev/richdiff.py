"""GitHub-style "rich" markdown diff.

The documents are split into top-level markdown blocks (headings, paragraphs,
lists, tables, fenced code, ...). Blocks are diffed as units so each one still
renders as valid markdown, then similar paragraph-like blocks get an inline
word-level diff of their rendered HTML.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

RenderFn = Callable[[str], str]

# Unchanged blocks this close to a change stay visible in the compact view.
CONTEXT_BLOCKS = 1
# Shorter unchanged runs are shown rather than collapsed behind a gap marker.
MIN_HIDDEN_RUN = 2
# Minimum similarity for an old/new block pair to be shown as one inline edit
# rather than a full removal followed by a full addition.
MODIFIED_THRESHOLD = 0.45

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_AZURE_FENCE_RE = re.compile(r"^\s{0,3}:::")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_LIST_RE = re.compile(r"^\s{0,3}([-*+]|\d{1,9}[.)])\s")
_ORDERED_MARKER_RE = re.compile(r"^(\s{0,3})\d{1,9}([.)])")
_REF_DEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S")
_TOKEN_RE = re.compile(r"<[^>]*>|&[#\w]+;|[\w'’-]+|\s+|[^\w\s<&]+|&")
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "wbr", "col", "area", "source"}
_TAG_NAME_RE = re.compile(r"^</?\s*([a-zA-Z][\w-]*)")


@dataclass
class _Block:
    source: str
    kind: str  # heading | code | list | text
    # Display number of an ordered list item, which is rendered on its own.
    ordinal: Optional[int] = None

    @property
    def key(self) -> str:
        key = "\n".join(line.rstrip() for line in self.source.strip().splitlines())
        if self.kind == "list":
            # Renumbering an ordered list should not make every item look changed.
            key = _ORDERED_MARKER_RE.sub(r"\1#\2", key, count=1)
        return key


def _block_kind(first_line: str) -> str:
    if _HEADING_RE.match(first_line):
        return "heading"
    if _FENCE_RE.match(first_line) or _AZURE_FENCE_RE.match(first_line):
        return "code"
    if _LIST_RE.match(first_line):
        return "list"
    return "text"


def _split_list_items(lines: Sequence[str]) -> List[_Block]:
    """Split one list into its top-level items so each can be diffed on its own."""
    item_indent = len(lines[0]) - len(lines[0].lstrip())
    items: List[List[str]] = []
    for line in lines:
        indent = len(line) - len(line.lstrip())
        if _LIST_RE.match(line) and indent <= item_indent:
            items.append([line])
        elif items:
            items[-1].append(line)

    blocks: List[_Block] = []
    next_number: Optional[int] = None
    for item in items:
        ordered = _ORDERED_MARKER_RE.match(item[0])
        ordinal = None
        if ordered:
            if next_number is None:
                next_number = int(re.match(r"\s*(\d+)", item[0]).group(1))
            ordinal = next_number
            next_number += 1
        else:
            next_number = None
        blocks.append(_Block("\n".join(item).strip("\n"), "list", ordinal))
    return blocks


def split_blocks(text: str) -> List[_Block]:
    """Split markdown into top-level blocks that render correctly on their own."""
    lines = (text or "").splitlines()
    blocks: List[_Block] = []
    current: List[str] = []
    pending_blank = False

    def flush() -> None:
        nonlocal current
        if any(line.strip() for line in current):
            if _block_kind(current[0]) == "list":
                blocks.extend(_split_list_items(current))
            else:
                blocks.append(_Block("\n".join(current).strip("\n"), _block_kind(current[0])))
        current = []

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        azure_fence = not fence and _AZURE_FENCE_RE.match(line)
        if fence or azure_fence:
            flush()
            fence_lines = [line]
            marker = fence.group(1)[0] * 3 if fence else ":::"
            i += 1
            while i < len(lines):
                fence_lines.append(lines[i])
                i += 1
                if lines[i - 1].strip().startswith(marker):
                    break
            blocks.append(_Block("\n".join(fence_lines), "code"))
            pending_blank = False
            continue

        if not line.strip():
            pending_blank = True
            i += 1
            continue

        if _REF_DEF_RE.match(line):
            # Link reference definitions are re-appended to every block at render time.
            i += 1
            continue

        if _HEADING_RE.match(line):
            flush()
            blocks.append(_Block(line, "heading"))
            pending_blank = False
            i += 1
            continue

        if pending_blank and current:
            indented = line.startswith(("  ", "\t"))
            continues_list = _block_kind(current[0]) == "list" and (indented or _LIST_RE.match(line))
            if continues_list:
                current.append("")
            else:
                flush()
        current.append(line)
        pending_blank = False
        i += 1

    flush()
    return blocks


def _ref_definitions(text: str) -> str:
    refs = [line for line in (text or "").splitlines() if _REF_DEF_RE.match(line)]
    return "\n\n" + "\n".join(refs) if refs else ""


def _tokenize(html_text: str) -> List[str]:
    return _TOKEN_RE.findall(html_text)


def _is_tag(token: str) -> bool:
    return token.startswith("<")


def _tags_balanced(tokens: Sequence[str]) -> bool:
    stack: List[str] = []
    for token in tokens:
        if not _is_tag(token) or token.startswith(("<!", "<?")) or token.endswith("/>"):
            continue
        match = _TAG_NAME_RE.match(token)
        if not match:
            continue
        name = match.group(1).lower()
        if name in _VOID_TAGS:
            continue
        if token.startswith("</"):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _wrap_text_runs(tokens: Sequence[str], wrapper: str, keep_tags: bool) -> str:
    out: List[str] = []
    run: List[str] = []

    def flush_run() -> None:
        if run:
            text = "".join(run)
            out.append(f"<{wrapper}>{text}</{wrapper}>" if text.strip() else text)
            run.clear()

    for token in tokens:
        if _is_tag(token):
            flush_run()
            if keep_tags:
                out.append(token)
        else:
            run.append(token)
    flush_run()
    return "".join(out)


def inline_html_diff(old_html: str, new_html: str) -> str:
    """Merge two HTML fragments, marking word changes with <del>/<ins>.

    The new fragment's structure is always kept; removed markup is only
    re-emitted when it is self-contained, so the result stays well-formed.
    """
    old_tokens = _tokenize(old_html)
    new_tokens = _tokenize(new_html)
    matcher = difflib.SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    out: List[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append("".join(new_tokens[j1:j2]))
            continue
        if tag in ("delete", "replace"):
            removed = old_tokens[i1:i2]
            out.append(_wrap_text_runs(removed, "del", keep_tags=_tags_balanced(removed)))
        if tag in ("insert", "replace"):
            out.append(_wrap_text_runs(new_tokens[j1:j2], "ins", keep_tags=True))
    return "".join(out)


def _similarity(old: _Block, new: _Block) -> float:
    if old.kind != new.kind or old.kind == "code":
        return 0.0
    old_words, new_words = old.key.split(), new.key.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    if matcher.real_quick_ratio() < MODIFIED_THRESHOLD:
        return 0.0
    return matcher.ratio()


# (status, old_block, new_block); status is equal | added | removed | modified
_Op = Tuple[str, Optional[_Block], Optional[_Block]]


def _pair_replaced(old: Sequence[_Block], new: Sequence[_Block]) -> List[_Op]:
    """Align a replaced run of blocks, pairing similar ones as in-place edits."""
    ops: List[_Op] = []
    j = 0
    for old_block in old:
        best_index, best_score = -1, MODIFIED_THRESHOLD
        for k in range(j, len(new)):
            score = _similarity(old_block, new[k])
            if score >= best_score:
                best_index, best_score = k, score
        if best_index < 0:
            ops.append(("removed", old_block, None))
            continue
        ops.extend(("added", None, block) for block in new[j:best_index])
        ops.append(("modified", old_block, new[best_index]))
        j = best_index + 1
    ops.extend(("added", None, block) for block in new[j:])
    return ops


def diff_blocks(base_text: str, head_text: str) -> List[_Op]:
    old_blocks = split_blocks(base_text)
    new_blocks = split_blocks(head_text)
    matcher = difflib.SequenceMatcher(
        None,
        [block.key for block in old_blocks],
        [block.key for block in new_blocks],
        autojunk=False,
    )
    ops: List[_Op] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.extend(("equal", old, new) for old, new in zip(old_blocks[i1:i2], new_blocks[j1:j2]))
        elif tag == "delete":
            ops.extend(("removed", block, None) for block in old_blocks[i1:i2])
        elif tag == "insert":
            ops.extend(("added", None, block) for block in new_blocks[j1:j2])
        else:
            ops.extend(_pair_replaced(old_blocks[i1:i2], new_blocks[j1:j2]))
    return ops


_LABELS = {"added": "Added", "removed": "Removed", "changed": "Changed"}
_INS_DEL_RE = re.compile(r"<(ins|del)>.*?</\1>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)


def _change_kind(merged_html: str) -> str:
    """Classify an inline-diffed fragment as equal, added, removed or changed."""
    has_ins = "<ins>" in merged_html
    has_del = "<del>" in merged_html
    if not has_ins and not has_del:
        return "equal"
    untouched = _TAG_RE.sub("", _INS_DEL_RE.sub("", merged_html)).strip()
    if has_ins and not has_del:
        return "added" if not untouched else "changed-added"
    if has_del and not has_ins:
        return "removed" if not untouched else "changed-removed"
    return "changed"


def _mark_table_rows(merged_html: str) -> str:
    def mark(match: "re.Match[str]") -> str:
        kind = _change_kind(match.group(1))
        if kind == "equal":
            return match.group(0)
        return f'<tr class="rd-row-{kind.split("-")[-1]}">{match.group(1)}</tr>'

    return _TABLE_ROW_RE.sub(mark, merged_html)


def _render_block(block: _Block, refs: str, render: RenderFn) -> str:
    rendered = render(block.source + refs)
    if block.ordinal is not None and block.ordinal != 1:
        rendered = rendered.replace("<ol>", f'<ol start="{block.ordinal}">', 1)
    return rendered


def _wrap(status: str, body: str, hidden: bool, bar: Optional[str] = None) -> str:
    """Wrap a rendered block; ``bar`` picks the left edge colour (added/removed/changed)."""
    bar = bar or (status if status in ("added", "removed") else None)
    classes = f"rd-block rd-{status}"
    if bar:
        classes += f" rd-bar-{bar}"
    if hidden:
        classes += " rd-far"
    label = _LABELS.get(bar or "")
    title = f' title="{label}"' if label else ""
    return f'<div class="{classes}"{title}>{body}</div>'


def _gap(count: int) -> str:
    noun = "block" if count == 1 else "blocks"
    return (
        f'<button type="button" class="rd-gap" title="Show the whole document">'
        f"⋯ {count} unchanged {noun}</button>"
    )


def render_rich_diff(base_text: Optional[str], head_text: Optional[str], render: RenderFn) -> str:
    """Render a block-level markdown diff.

    Unchanged blocks far from any change get the ``rd-far`` class and are
    preceded by an ``rd-gap`` marker so the UI can hide them in the compact
    "changes only" view. The nearest preceding heading of every change is
    always kept visible so each change has its section for context.
    """
    base = base_text or ""
    head = head_text or ""
    if not base.strip() and not head.strip():
        return "<p><em>No content available.</em></p>"

    ops = diff_blocks(base, head)
    changed = [index for index, (status, _, _) in enumerate(ops) if status != "equal"]
    if not changed:
        return "<p><em>No rendered changes (whitespace or formatting only).</em></p>"

    visible = set(changed)
    for index in changed:
        visible.update(range(max(0, index - CONTEXT_BLOCKS), min(len(ops), index + CONTEXT_BLOCKS + 1)))
        for back in range(index - 1, -1, -1):
            status, _, new_block = ops[back]
            if status == "equal" and new_block is not None and new_block.kind == "heading":
                visible.add(back)
                break

    run_start = None
    for index in range(len(ops) + 1):
        if index < len(ops) and index not in visible:
            run_start = index if run_start is None else run_start
        elif run_start is not None:
            if index - run_start < MIN_HIDDEN_RUN:
                visible.update(range(run_start, index))
            run_start = None

    old_refs = _ref_definitions(base)
    new_refs = _ref_definitions(head)

    parts: List[str] = []
    hidden_run = 0
    for index, (status, old_block, new_block) in enumerate(ops):
        bar = None
        if status in ("equal", "added"):
            body = _render_block(new_block, new_refs, render)
        elif status == "removed":
            body = _render_block(old_block, old_refs, render)
        else:
            body = inline_html_diff(
                _render_block(old_block, old_refs, render),
                _render_block(new_block, new_refs, render),
            )
            if "<table" in body:
                # Rows carry their own edge marks; a block-wide bar would be misleading.
                body = _mark_table_rows(body)
                bar = "none"
            else:
                bar = _change_kind(body).split("-")[-1]
                if bar == "equal":
                    status, bar = "equal", None

        hidden = index not in visible
        if hidden:
            if hidden_run == 0:
                run_length = 0
                while index + run_length < len(ops) and index + run_length not in visible:
                    run_length += 1
                parts.append(_gap(run_length))
            hidden_run += 1
        else:
            hidden_run = 0
        parts.append(_wrap(status, body, hidden, bar))

    return "".join(parts)

