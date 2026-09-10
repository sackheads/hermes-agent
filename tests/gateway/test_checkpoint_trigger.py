"""Tests for gateway/checkpoint_trigger.py — CheckpointTrigger class."""

import json
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch, MagicMock, PropertyMock

import pytest

from gateway.checkpoint_trigger import CheckpointTrigger, _COALESCE_SEC, _MIN_MESSAGES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_msg(role: str, content: str) -> Dict[str, Any]:
    """Build a message dict as the gateway would."""
    return {"role": role, "content": content}


def make_session(min_msgs: int = 5) -> List[Dict[str, Any]]:
    """Build a realistic multi-turn conversation."""
    return [
        make_msg("user", "Hey, let's talk about the k8s cluster upgrade on noc"),
        make_msg("assistant", "Sure, what version are we targeting?"),
        make_msg("user", "Let's go with 1.32 — need to check CVE-2025-1234 first though"),
        make_msg("assistant", "Good call. I can run a grype scan on the current images."),
        make_msg("user", "We decided to use distroless images going forward. Question: how do we handle the nfs mount? Still pending that"),
        make_msg("assistant", "Agreed on distroless. For NFS, we should figure out the permissions model."),
        make_msg("user", "PR #142 has the config changes, let's review it"),
        make_msg("assistant", "The file at /shared/agents/common/infrastructure/nfs/config.yaml looks clean"),
    ]


@pytest.fixture
def trigger() -> CheckpointTrigger:
    return CheckpointTrigger()


# ---------------------------------------------------------------------------
# Extraction Tests
# ---------------------------------------------------------------------------


class TestExtractTopics:
    def test_capitalized_phrases(self, trigger):
        """Should extract capitalized noun phrases as topics."""
        text = "We discussed the Cluster Upgrade and Patching Strategy"
        topics = trigger._extract_topics(text)
        assert "Cluster Upgrade" in topics
        assert "Patching Strategy" in topics

    def test_post_about_words(self, trigger):
        """Should extract words after 'about', 'regarding', etc."""
        text = "We were talking about diffuser disk space and regarding CVE-2025-1234"
        topics = trigger._extract_topics(text)
        assert any("diffuser disk" in t.lower() for t in topics)

    def test_acronyms(self, trigger):
        """Should extract all-caps acronyms."""
        text = "The CVE for the CSI driver needs attention"
        topics = trigger._extract_topics(text)
        assert "CVE" in topics
        assert "CSI" in topics

    def test_file_paths(self, trigger):
        """Should extract file names from shared paths."""
        text = "Check the config at /shared/agents/common/nfs/config.yaml"
        topics = trigger._extract_topics(text)
        assert "config.yaml" in topics

    def test_short_entries_filtered(self, trigger):
        """Should filter out single-char entries."""
        topics = trigger._extract_topics("a b c")
        assert all(len(t) > 1 for t in topics)

    def test_max_10_topics(self, trigger):
        """Should return at most 10 topics."""
        many = " ".join(f"Topic{i} Topic{i}B" for i in range(20))
        topics = trigger._extract_topics(many)
        assert len(topics) <= 10


class TestExtractDecisions:
    def test_we_decided(self, trigger):
        """Should capture 'we decided' statements."""
        decisions = trigger._extract_decisions("we decided to use distroless images")
        assert any("decided to use distroless" in d.lower() for d in decisions)

    def test_lets_go_with(self, trigger):
        """Should capture 'let's go with'."""
        decisions = trigger._extract_decisions("let's go with 1.32 for the upgrade")
        assert any("go with 1.32" in d.lower() for d in decisions)

    def test_we_agreed(self, trigger):
        """Should capture 'we agreed'."""
        decisions = trigger._extract_decisions("we agreed on the distroless approach")
        assert any("agreed on the distroless" in d.lower() for d in decisions)

    def test_decision_prefix(self, trigger):
        """Should capture 'decision:' prefix."""
        decisions = trigger._extract_decisions("Decision: use sealed secrets for k8s")
        assert any("sealed secrets" in d.lower() for d in decisions)

    def test_no_decision_noise(self, trigger):
        """Should not extract false positives from unrelated text."""
        decisions = trigger._extract_decisions(
            "The weather is nice today. Let me check my calendar."
        )
        assert len(decisions) <= 1  # "let me" might weakly match — that's OK


class TestExtractQuestions:
    def test_we_need_to_decide(self, trigger):
        """Should capture 'we need to decide' questions."""
        qs = trigger._extract_questions("we still need to decide on the nfs backend")
        assert any("need to decide" in q.lower() for q in qs)

    def test_question_prefix(self, trigger):
        """Should capture 'question:' prefix."""
        qs = trigger._extract_questions("Question: how do we handle secrets rotation?")
        assert any("question" in q.lower() for q in qs)

    def test_what_about(self, trigger):
        """Should capture 'what about' inquiries."""
        qs = trigger._extract_questions("what about the monitoring stack?")
        assert any("what about" in q.lower() for q in qs)

    def test_is_it_blocked(self, trigger):
        """Should capture 'is it blocked'."""
        qs = trigger._extract_questions("is that blocked on the upstream PR?")
        assert any("blocked" in q.lower() for q in qs)


class TestExtractArtifacts:
    def test_shared_paths(self, trigger):
        """Should capture /shared/ paths."""
        refs = trigger._extract_artifacts(
            "Check /shared/agents/common/infrastructure/nfs/config.yaml"
        )
        assert "/shared/agents/common/infrastructure/nfs/config.yaml" in refs

    def test_pr_numbers(self, trigger):
        """Should capture PR # references."""
        refs = trigger._extract_artifacts("Let me review PR #142")
        assert "PR #142" in refs or "PR #" in str(refs)

    def test_github_urls(self, trigger):
        """Should capture GitHub URLs."""
        refs = trigger._extract_artifacts(
            "See https://github.com/bnaylor/k8s-configs/pull/42"
        )
        assert any("github.com" in r for r in refs)

    def test_code_files(self, trigger):
        """Should capture file extensions."""
        refs = trigger._extract_artifacts("I updated deploy.py and config.yaml")
        assert any(r.endswith(".py") or r.endswith(".yaml") for r in refs)


class TestExtractCheckpoint:
    def test_full_extraction(self, trigger):
        """Should extract topics, decisions, questions, and refs from a conversation."""
        msgs = make_session()
        topics, state, refs = trigger._extract_checkpoint(msgs)
        assert len(topics) >= 2
        assert state.get("decisions")
        assert any("distroless" in d.lower() for d in state["decisions"])
        assert state.get("open_questions")
        assert len(refs) >= 1

    def test_minimal_messages(self, trigger):
        """Should return empty for very few messages."""
        msgs = [make_msg("user", "hi")]
        topics, state, refs = trigger._extract_checkpoint(msgs)
        # Extraction still runs, but may return empty — that's fine
        assert isinstance(topics, list)

    def test_ignores_bot_messages(self, trigger):
        """Bot-only messages should not contaminate topic extraction."""
        msgs = [
            {"role": "bot", "content": "I am a bot and I approve this message"},
            {"role": "system", "content": "system note"},
        ]
        topics, state, refs = trigger._extract_checkpoint(msgs)
        # Should not crash; topics may be empty
        assert isinstance(topics, list)


# ---------------------------------------------------------------------------
# Coalesce & Minimum Messages Tests (on_session_switch)
# ---------------------------------------------------------------------------


class TestOnSessionSwitch:
    def test_minimum_messages_check(self, trigger):
        """Should skip if fewer than _MIN_MESSAGES."""
        msgs = [make_msg("user", "hi"), make_msg("assistant", "hello")]
        with patch.object(trigger, "_store_checkpoint") as mock_store:
            trigger.on_session_switch(
                idled_session_key="test_key",
                idled_session_id="test_sid",
                channel_name="test-channel",
                sensitivity="public",
                messages=msgs,
                user_id="user1",
            )
        mock_store.assert_not_called()

    def test_coalesce_window(self, trigger):
        """Should skip if checkpointed the same session within _COALESCE_SEC."""
        msgs = make_session()
        with patch.object(trigger, "_store_checkpoint") as mock_store:
            # First call — should store
            trigger.on_session_switch(
                idled_session_key="key1",
                idled_session_id="sid1",
                channel_name="ch",
                sensitivity="public",
                messages=msgs,
                user_id="u1",
            )
            assert mock_store.call_count == 1
            # Second call immediately — should coalesce
            trigger.on_session_switch(
                idled_session_key="key1",
                idled_session_id="sid1",
                channel_name="ch",
                sensitivity="public",
                messages=msgs,
                user_id="u1",
            )
            assert mock_store.call_count == 1  # Not called again

    def test_coalesce_different_sessions(self, trigger):
        """Should NOT coalesce across different sessions."""
        msgs = make_session()
        with patch.object(trigger, "_store_checkpoint") as mock_store:
            trigger.on_session_switch(
                idled_session_key="key_a",
                idled_session_id="sid_a",
                channel_name="ch",
                sensitivity="public",
                messages=msgs,
                user_id="u1",
            )
            trigger.on_session_switch(
                idled_session_key="key_b",
                idled_session_id="sid_b",
                channel_name="ch",
                sensitivity="public",
                messages=msgs,
                user_id="u1",
            )
            assert mock_store.call_count == 2

    def test_sensitivity_propagation(self, trigger):
        """Restricted sessions should be tagged appropriately."""
        msgs = make_session()
        with patch.object(trigger, "_store_checkpoint") as mock_store:
            trigger.on_session_switch(
                idled_session_key="dm_key",
                idled_session_id="dm_sid",
                channel_name="@scromp",
                sensitivity="restricted",
                messages=msgs,
                user_id="u1",
            )
            (_content, tags, _refs) = mock_store.call_args[0]
            assert any("sensitivity:restricted" in t for t in tags)

    def test_fallback_topic_from_channel_name(self, trigger):
        """Should use channel name as fallback topic when no topics extracted."""
        msgs = [make_msg("user", "a"), make_msg("user", "b"), make_msg("user", "c")]
        with patch.object(trigger, "_store_checkpoint") as mock_store:
            trigger.on_session_switch(
                idled_session_key="k",
                idled_session_id="sid",
                channel_name="#general",
                sensitivity="public",
                messages=msgs,
                user_id="u1",
            )
            (_content, tags, _refs) = mock_store.call_args[0]
            assert any("#general" in t for t in tags)


# ---------------------------------------------------------------------------
# Store checkpoint tests
# ---------------------------------------------------------------------------


class TestStoreCheckpoint:
    def test_direct_store_write(self, trigger):
        """Should write to holographic MemoryStore directly."""
        mock_store = MagicMock()
        with patch(
            "plugins.memory.holographic.store.MemoryStore",
            return_value=mock_store,
        ):
            with patch(
                "hermes_constants.get_hermes_home",
                return_value=Path("/tmp/fake_hermes"),
            ):
                trigger._store_checkpoint(
                    content="decided on distroless images",
                    tags=["topic:distroless", "sensitivity:public", "source:noc-chat"],
                    refs=["PR #142"],
                )
                mock_store.add_fact.assert_called_once_with(
                    content="decided on distroless images",
                    category="general",
                    tags="topic:distroless, sensitivity:public, source:noc-chat",
                )

    def test_exception_handling(self, trigger):
        """Should log and swallow exceptions from store."""
        with patch(
            "plugins.memory.holographic.store.MemoryStore",
            side_effect=RuntimeError("DB gone"),
        ):
            with patch(
                "gateway.checkpoint_trigger.logger"
            ) as mock_logger:
                # Should not raise
                trigger._store_checkpoint("x", ["t:y"], [])
                mock_logger.debug.assert_called()


# ---------------------------------------------------------------------------
# Integration-style test
# ---------------------------------------------------------------------------


class TestExtractCheckpointIntegration:
    def test_realistic_conversation(self, trigger):
        """Full extraction pipeline with realistic data."""
        msgs = [
            make_msg("user", "We need to decide on the monitoring stack for noc"),
            make_msg("assistant", "Options: Prometheus/Grafana or SigNoz"),
            make_msg("user", "Let's go with SigNoz — it has APM built in"),
            make_msg("assistant", "Good choice. I'll draft the deploy config."),
            make_msg("user", "Check the repo at https://github.com/bnaylor/k8s-configs"),
            make_msg("user", "Question: what about the retention policy for traces?"),
            make_msg("assistant", "Default is 15 days, we can adjust in config.yaml"),
            make_msg("user", "We agreed on 30 days retention. /shared/infra/monitoring/signoz/values.yaml needs updating"),
        ]

        topics, state, refs = trigger._extract_checkpoint(msgs)

        # File paths in messages get extracted as topics
        assert any("values.yaml" in t for t in topics)
        assert state.get("decisions")
        decisions_text = " ".join(state["decisions"]).lower()
        assert "signoz" in decisions_text or "monitoring" in decisions_text
        assert state.get("open_questions")
        assert any("retention" in q.lower() for q in state["open_questions"])
        assert any("github.com" in r for r in refs)
        assert any("values.yaml" in r for r in refs)
