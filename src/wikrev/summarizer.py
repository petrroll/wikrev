from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .config import WIKREV_DIR

logger = logging.getLogger(__name__)

CACHE_PATH = WIKREV_DIR / "summary_cache.json"

# Keep prompts bounded so one huge diff can't stall or blow up a summary request.
MAX_DIFF_CHARS = 60_000


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Ignoring unreadable summary cache: %s", CACHE_PATH)
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def get_cached_summary(key: str) -> Optional[str]:
    cache = _load_cache()
    return cache.get(key)


def set_cached_summary(key: str, summary: str) -> None:
    cache = _load_cache()
    cache[key] = summary
    _save_cache(cache)


def _build_summary_prompt(diff_text: str) -> str:
    truncated = diff_text or ""
    if len(truncated) > MAX_DIFF_CHARS:
        truncated = truncated[:MAX_DIFF_CHARS] + "\n\n[diff truncated for length]"
    return (
        "Summarize the following markdown diff in 1-2 sentences. "
        "Focus on user-visible changes. If content appears in both deletions (-) "
        "and additions (+) with the same text, it may have been moved or "
        "reformatted rather than truly added or removed. Be precise about "
        "whether content was added, removed, moved, or modified.\n\n"
        f"{truncated}"
    )


def _approve_read_only_permissions(request: Any, invocation: dict[str, str]) -> Any:
    from copilot import PermissionRequestResult

    del invocation

    kind = getattr(getattr(request, "kind", None), "value", None)
    if kind == "read" or getattr(request, "read_only", False):
        return PermissionRequestResult(kind="approved")

    return PermissionRequestResult(
        kind="denied-no-approval-rule-and-could-not-request-from-user",
        message="WikRev only approves read-only Copilot permissions for summaries.",
    )


async def summarize_with_copilot(diff_text: str, model: str) -> str:
    try:
        from copilot import CopilotClient
    except Exception as exc:
        logger.exception("Failed to import Copilot SDK")
        raise RuntimeError(
            "Copilot SDK is not available. Install the GitHub Copilot SDK for Python or disable summaries."
        ) from exc

    client = CopilotClient()
    response_event = None
    try:
        await client.start()
        async with await client.create_session(
            {
                "model": model,
                "on_permission_request": _approve_read_only_permissions,
                "system_message": {
                    "mode": "append",
                    "content": (
                        "You are summarizing the diff provided in the prompt. "
                        "Do not use shell, write, or network tools. If you need "
                        "extra context, only use read-only tools."
                    ),
                },
            }
        ) as session:
            response_event = await session.send_and_wait(
                {"prompt": _build_summary_prompt(diff_text)},
                timeout=120,
            )
    except Exception:
        logger.exception("Error during Copilot summarization")
        raise
    finally:
        try:
            await client.stop()
        except Exception as exc:
            logger.warning("Error stopping Copilot client: %s", exc)

    response_text = getattr(getattr(response_event, "data", None), "content", "") or ""
    if not response_text.strip():
        raise RuntimeError("Copilot did not return a summary.")
    return response_text.strip()
