"""
CheckpointTrigger — extract topics/decisions/artifacts from idled sessions
and store as structured facts in Holographic memory.

Triggered by the gateway when detect_switch() fires with an idle gap >= 1 min.
Runs as a fire-and-forget daemon thread — never blocks message delivery.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Coalesce window: don't re-checkpoint the same session within this many seconds
_COALESCE_SEC = 120

# Minimum messages in a session to bother extracting
_MIN_MESSAGES = 3

# Patterns for decision language
_DECISION_PATTERNS = [
    r"(?:we\s+(?:decided|agreed|settled|chose|went\s+with|picked))",
    r"(?:let's\s+(?:go\s+with|do|use|try|stick\s+with))",
    r"(?:decision:?)\s+(.+)",
    r"(?:resolved|concluded)\s+(?:that\s+)?(.+)",
    r"(?:we'll\s+do)\s+(.+)",
]

# Patterns for open questions / unresolved items
_QUESTION_PATTERNS = [
    r"(?:we\s+(?:need\s+to|still\s+need|should))\s+(?:decide|figure|determine|resolve|look\s+into)",
    r"(?:is\s+that|is\s+it|are\s+we)\s+(?:blocked|stalled|pending)",
    r"(?:question|open\s+issue):?\s+(.+)",
    r"(?:what\s+about|how\s+(?:about|should|do\s+we))",
    r"(?:still\s+(?:need|pending|outstanding))",
    r"(?:TODO|todo|to-do):?\s+(.+)",
]

# Patterns for artifact references
_ARTIFACT_PATTERNS = [
    r"/shared/[^\s,;)]+",
    r"PR\s+#?\d+",
    r"issue\s+#?\d+",
    r"https://github\.com/[^\s,;)]+",
    r"[^\s]+\.(?:go|py|js|ts|yaml|yml|toml|json|md)",
]


class CheckpointTrigger:
    """Lightweight trigger: extracts context from idled sessions and stores in Holographic."""

    def __init__(self) -> None:
        self._last_checkpoint: Dict[str, float] = {}  # session_key → timestamp

    def on_session_switch(
        self,
        *,
        idled_session_key: str,
        idled_session_id: str,
        channel_name: str,
        sensitivity: str,
        messages: List[Dict[str, Any]],
        user_id: str,
    ) -> None:
        """Called from gateway daemon thread when user switches from an idle session.

        1. Check coalesce window — skip if checkpointed this session recently
        2. Skip if too few messages (< _MIN_MESSAGES)
        3. Extract topics, decisions, artifact refs
        4. Call fact_store to persist as structured facts
        5. Log the checkpoint for validation
        """
        # Coalesce check
        now = time.time()
        last = self._last_checkpoint.get(idled_session_key, 0)
        if now - last < _COALESCE_SEC:
            logger.debug(
                "CHECKPOINT:coalesced session=%s (%.0fs since last)",
                idled_session_key, now - last,
            )
            return
        self._last_checkpoint[idled_session_key] = now

        # Minimum messages check
        if not messages or len(messages) < _MIN_MESSAGES:
            logger.debug(
                "CHECKPOINT:skip session=%s (%d msgs, min %d)",
                idled_session_key, len(messages) if messages else 0, _MIN_MESSAGES,
            )
            return

        # Extract
        topics, state_json, artifact_refs = self._extract_checkpoint(messages)

        if not topics:
            logger.info(
                "CHECKPOINT:session=%s channel=%s no topics extracted (fallback to channel name)",
                idled_session_key, channel_name,
            )
            # Use channel name as a weak topic anchor
            topics = [channel_name.split("/")[-1].strip()]

        # Build fact content
        state_parts = []
        if state_json.get("decisions"):
            state_parts.append("decisions: " + "; ".join(state_json["decisions"]))
        if state_json.get("open_questions"):
            state_parts.append("open: " + "; ".join(state_json["open_questions"]))
        fact_content = "; ".join(state_parts) if state_parts else f"discussed {', '.join(topics[:3])}"

        # Build tags
        tags_parts = [
            f"source:{channel_name.split('/')[-1]}",
            f"sensitivity:{sensitivity}",
            f"source_session:{idled_session_key}",
        ]
        tags_parts.extend(f"topic:{t}" for t in topics[:5])

        # Log the checkpoint
        logger.info(
            "CHECKPOINT:[%s] session=%s channel=%s "
            "topics=%s decisions=%d open_qs=%d refs=%d",
            sensitivity,
            idled_session_key,
            channel_name,
            topics[:5],
            len(state_json.get("decisions", [])),
            len(state_json.get("open_questions", [])),
            len(artifact_refs),
        )

        # Store in Holographic via fact_store
        self._store_checkpoint(fact_content, tags_parts, artifact_refs)

    def _extract_checkpoint(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
        """Lightweight extraction via regex + pattern matching.

        Returns (topics, state_json, artifact_refs).
        """
        # Filter to user messages only for topic extraction
        user_texts = []
        all_texts = []
        for m in messages:
            role = (m.get("role") or m.get("author") or "").lower()
            text = m.get("content", m.get("text", m.get("message", "")))
            if not text or not isinstance(text, str):
                continue
            all_texts.append(text)
            if role in ("user", "human", ""):
                user_texts.append(text)

        combined = " ".join(all_texts)
        user_combined = " ".join(user_texts)

        # Extract topics: capitalized noun phrases, significant words
        topics = self._extract_topics(combined)

        # Extract decisions
        decisions = self._extract_decisions(combined)

        # Extract open questions
        open_questions = self._extract_questions(combined)

        # Extract artifact references
        artifact_refs = self._extract_artifacts(combined)

        state_json = {}
        if decisions:
            state_json["decisions"] = decisions
        if open_questions:
            state_json["open_questions"] = open_questions

        return topics, state_json, artifact_refs

    def _extract_topics(self, text: str) -> List[str]:
        """Extract topic-like noun phrases."""
        topics = set()

        # Capitalized multi-word phrases (proper nouns, project names)
        caps_phrases = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
        for p in caps_phrases:
            if len(p.split()) <= 4:  # avoid overly long phrases
                topics.add(p.strip())

        # Words after "about", "regarding", "on topic of", "talking about"
        for match in re.finditer(
            r"(?:about|regarding|on\s+(?:the\s+)?topic\s+of|talking\s+about|discussing)\s+"
            r"([A-Za-z][A-Za-z0-9_\-/]+(?:\s+[A-Za-z][A-Za-z0-9_\-/]+){0,3})",
            text, re.IGNORECASE,
        ):
            topics.add(match.group(1).strip())

        # Acronyms and tech terms
        for match in re.finditer(r"\b[A-Z]{2,}(?:\d*)\b", text):
            topics.add(match.group(0))

        # File paths as topics
        for match in re.finditer(r"/shared/[^\s,;)]+", text):
            path = match.group(0)
            name = path.rsplit("/", 1)[-1]
            if name:
                topics.add(name)

        # Remove very short entries and duplicates
        return sorted([t for t in topics if len(t) > 1], key=lambda t: -len(t))[:10]

    def _extract_decisions(self, text: str) -> List[str]:
        """Extract decision-like statements."""
        decisions = []
        for pattern in _DECISION_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                # Grab the sentence fragment around the match
                start = max(0, match.start() - 20)
                end = min(len(text), match.end() + 60)
                snippet = text[start:end].strip()
                # Clean up to sentence boundary
                if snippet and len(snippet) > 10:
                    decisions.append(snippet[:120].strip())
        return decisions[:5]

    def _extract_questions(self, text: str) -> List[str]:
        """Extract open questions and unresolved items."""
        questions = []
        for pattern in _QUESTION_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                end = min(len(text), match.end() + 60)
                snippet = text[match.start():end].strip()
                if snippet:
                    questions.append(snippet[:120].strip())
        return questions[:5]

    def _extract_artifacts(self, text: str) -> List[str]:
        """Extract artifact references (files, PRs, URLs)."""
        refs = set()
        for pattern in _ARTIFACT_PATTERNS:
            for match in re.finditer(pattern, text):
                refs.add(match.group(0).strip())
        return sorted(refs)[:10]

    def _store_checkpoint(
        self, content: str, tags: List[str], refs: List[str]
    ) -> None:
        """Store checkpoint facts directly into Holographic DB.

        Opens a direct connection to the memory_store.db so the gateway can
        write facts with proper tags without going through the agent's
        MemoryManager (which doesn't exist in gateway context).
        Falls back to logging-only shadow mode if the DB is inaccessible.
        """
        try:
            from plugins.memory.hermes_memory.store import MemoryStore
            from hermes_constants import get_hermes_home
            db_path = get_hermes_home() / "memory_store.db"
            store = MemoryStore(db_path=str(db_path))
            tags_str = ", ".join(tags)
            store.add_fact(content=content, category="general", tags=tags_str)
            logger.debug(
                "CHECKPOINT:stored (tags=%s): %.80s",
                tags_str, content,
            )
        except Exception as e:
            logger.debug("CHECKPOINT:store failed (non-fatal) — %s", e)


# Singleton
_trigger: Optional[CheckpointTrigger] = None
_trigger_lock = threading.Lock()


def get_checkpoint_trigger() -> Optional[CheckpointTrigger]:
    """Return the global CheckpointTrigger singleton."""
    global _trigger
    if _trigger is not None:
        return _trigger
    with _trigger_lock:
        if _trigger is None:
            try:
                from hermes_cli.config import cfg_get, load_config
                config = load_config()
                if not cfg_get(config, "memory", "cross_channel_awareness"):
                    return None
                _trigger = CheckpointTrigger()
                logger.info("CHECKPOINT:trigger initialized")
            except Exception as e:
                logger.info("CHECKPOINT:trigger init failed: %s", e)
        return _trigger
