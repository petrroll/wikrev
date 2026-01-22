from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import WIKREV_DIR

logger = logging.getLogger(__name__)

CACHE_PATH = WIKREV_DIR / "summary_cache.json"


@dataclass
class SummaryResult:
    text: str
    from_cache: bool


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))


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


def _find_copilot_cli_path() -> str:
    """Find the copilot CLI executable path, handling Windows quirks."""
    import shutil
    import sys
    
    # Try to find copilot in PATH
    copilot_path = shutil.which("copilot")
    if copilot_path:
        logger.debug("Found copilot at: %s", copilot_path)
        return copilot_path
    
    # On Windows, also check for .cmd/.bat variants
    if sys.platform == "win32":
        for ext in [".cmd", ".bat", ".exe"]:
            path = shutil.which(f"copilot{ext}")
            if path:
                logger.debug("Found copilot at: %s", path)
                return path
    
    # Fallback to default
    return "copilot"


async def summarize_with_copilot(diff_text: str, model: str) -> str:
    try:
        from copilot import CopilotClient
    except Exception as exc:
        logger.error("Failed to import Copilot SDK: %s\n%s", exc, traceback.format_exc())
        raise RuntimeError(
            "Copilot SDK is not available. Install the GitHub Copilot SDK for Python or disable summaries."
        ) from exc
    
    prompt = (
        "Summarize the following markdown diff in 1-2 sentences. "
        "Focus on user-visible changes.\n\n"
        f"{diff_text}"
    )

    client = None
    session = None
    try:
        # Find the copilot CLI path (handles Windows .cmd/.bat files)
        cli_path = _find_copilot_cli_path()
        logger.debug("Creating CopilotClient with cli_path: %s", cli_path)
        client = CopilotClient({"cli_path": cli_path})
        
        logger.debug("Starting CopilotClient...")
        await client.start()
        
        logger.debug("Creating session with model: %s", model)
        session = await client.create_session({"model": model})

        done = asyncio.Event()
        response_text = {"value": ""}
        error_holder = {"error": None}

        def on_event(event):
            logger.debug("Received event: %s", event.type)
            try:
                if event.type.value == "assistant.message":
                    response_text["value"] = event.data.content
                elif event.type.value == "session.idle":
                    done.set()
                elif event.type.value == "error":
                    error_holder["error"] = getattr(event, 'data', event)
                    logger.error("Copilot error event: %s", event)
                    done.set()
            except Exception as e:
                logger.error("Error in on_event handler: %s\n%s", e, traceback.format_exc())
                error_holder["error"] = e
                done.set()

        session.on(on_event)
        logger.debug("Sending prompt to Copilot...")
        await session.send({"prompt": prompt})
        await done.wait()

        if error_holder["error"]:
            raise RuntimeError(f"Copilot returned an error: {error_holder['error']}")

        return response_text["value"].strip()
    except Exception as exc:
        logger.error("Error during Copilot summarization: %s\n%s", exc, traceback.format_exc())
        raise
    finally:
        try:
            if session:
                await session.destroy()
        except Exception as e:
            logger.warning("Error destroying session: %s", e)
        try:
            if client:
                await client.stop()
        except Exception as e:
            logger.warning("Error stopping client: %s", e)
