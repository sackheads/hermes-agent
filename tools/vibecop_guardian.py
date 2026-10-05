"""vibecop Guardian mode — project-aware smart approvals (fleet patch).

A standalone module so the fleet's vibecop logic stays out of upstream's own
``tools/approval_smart.py`` (which carries a Codex-inspired guardian that is a
*different* design, not a replacement for this). When enabled
(``approvals.vibecop.enabled``) and a prompt file is present, the approval LLM
sees an editable project-specific system prompt plus a workspace snapshot and
the session's recent verdict history.

Prompt resolution (first hit wins):
  1. ``approvals.vibecop.prompt_path`` (explicit config path)
  2. ``<project-root>/.hermes/vibecop-prompt.md`` (per-project)
  3. ``~/.hermes/vibecop-prompt.md`` (global)

This module is fleet-specific (see ``docs/fleet/PATCHES.md``). The call sites in
``tools/approval_smart.py`` are marked with the sentinel pair
``# === VIBECOP START / END ===`` so a post-upgrade ``grep`` finds them.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools import approval_context as _ctx

logger = logging.getLogger("tools.vibecop")

# Rolling per-session activity buffer: session_key -> [{"tool", "input", "verdict"}, ...].
# Thread-safe; bounded by ``approvals.vibecop.activity_window``.
_session_activity: Dict[str, List[Dict[str, Any]]] = {}
_activity_lock = threading.Lock()


def get_config() -> dict:
    """Read the ``approvals.vibecop.*`` config block (empty dict if absent)."""
    return _ctx._get_approval_config().get("vibecop", {}) or {}


def is_enabled() -> bool:
    """Whether vibecop mode is enabled in config."""
    return bool(get_config().get("enabled", False))


def get_activity_window() -> int:
    """Number of recent verdicts to keep per session (config, default 10)."""
    try:
        return int(get_config().get("activity_window", 10))
    except (ValueError, TypeError):
        return 10


def record_activity(session_key: str, tool: str, input_text: str, verdict: str) -> None:
    """Append a tool-call + verdict pair to the session's rolling buffer."""
    window = get_activity_window()
    with _activity_lock:
        activities = _session_activity.setdefault(session_key, [])
        activities.append({"tool": tool, "input": (input_text or "")[:200], "verdict": verdict})
        if len(activities) > window:
            del activities[: len(activities) - window]


def get_recent_activity(session_key: Optional[str]) -> List[Dict[str, Any]]:
    """Thread-safe snapshot of a session's recent verdict history."""
    if not session_key:
        return []
    with _activity_lock:
        return list(_session_activity.get(session_key, []))


def clear_session_activity(session_key: str) -> None:
    """Drop activity tracking for a session."""
    with _activity_lock:
        _session_activity.pop(session_key, None)


def load_prompt() -> str:
    """Load the vibecop system prompt from file, or ``""`` when none is configured."""
    try:
        explicit = get_config().get("prompt_path", "") or ""
        if explicit:
            path = Path(explicit).expanduser()
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        try:
            from agent.coding_context import _git_root, _marker_root
            from agent.runtime_cwd import resolve_agent_cwd

            root = _git_root(resolve_agent_cwd()) or _marker_root(resolve_agent_cwd())
            if root:
                project_prompt = root / ".hermes" / "vibecop-prompt.md"
                if project_prompt.is_file():
                    return project_prompt.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        global_prompt = Path.home() / ".hermes" / "vibecop-prompt.md"
        if global_prompt.is_file():
            return global_prompt.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def get_project_context() -> str:
    """Build a workspace-snapshot block for the approval LLM, or ``""``."""
    try:
        from agent.coding_context import build_coding_workspace_block

        block = build_coding_workspace_block()
        if block:
            return f"Project context:\n{block}"
    except Exception:
        pass
    return ""


def format_recent_activity(activities: List[Dict[str, Any]]) -> str:
    """Format recent verdict history for the approval LLM prompt."""
    if not activities:
        return ""
    lines = ["Recent tool activity in this session (most recent first):"]
    for entry in reversed(activities[-5:]):
        inp = (entry.get("input") or "")[:120]
        lines.append(f"  [{entry.get('verdict', '?')}] {entry.get('tool', '?')}: {inp}")
    return "\n".join(lines)


def build_messages(
    command: str, description: str, session_key: Optional[str]
) -> Optional[Tuple[List[Dict[str, str]], int]]:
    """Return ``(messages, max_tokens)`` for the vibecop approval call.

    Returns ``None`` when vibecop is disabled or has no prompt file, signalling
    the caller to fall through to upstream's default smart-approval prompt.
    """
    if not is_enabled():
        return None
    prompt = load_prompt()
    if not prompt:
        return None

    # Lazy import avoids a module-level cycle (approval_smart calls build_messages).
    from tools.approval_smart import _strip_shell_comments

    user_parts = [
        f"Command: {_strip_shell_comments(command)}",
        f"Flagged reason: {description}",
    ]
    context = get_project_context()
    if context:
        user_parts.insert(0, context)
    activity = get_recent_activity(session_key)
    if activity:
        user_parts.append(format_recent_activity(activity))

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    return messages, 64
