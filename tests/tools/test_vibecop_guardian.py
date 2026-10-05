"""Behavior tests for the fleet's vibecop Guardian mode (tools/vibecop_guardian)."""

from unittest.mock import patch as mock_patch

import pytest

from tools import vibecop_guardian


@pytest.fixture(autouse=True)
def _clear_activity():
    """The module keeps a process-global activity buffer; isolate per test."""
    vibecop_guardian._session_activity.clear()
    yield
    vibecop_guardian._session_activity.clear()


def _config(approvals: dict):
    return mock_patch("hermes_cli.config.load_config_readonly", return_value={"approvals": approvals})


class TestBuildMessages:
    def test_disabled_returns_none(self):
        with _config({"mode": "smart", "vibecop": {"enabled": False}}):
            assert vibecop_guardian.build_messages("rm -rf /", "recursive delete", "s") is None

    def test_enabled_without_prompt_returns_none(self):
        with _config({"vibecop": {"enabled": True}}):
            assert vibecop_guardian.build_messages("rm -rf /", "recursive delete", "s") is None

    def test_enabled_with_prompt_builds_messages(self, monkeypatch):
        with _config({"vibecop": {"enabled": True}}):
            monkeypatch.setattr(vibecop_guardian, "load_prompt", lambda: "You are a vibecop reviewer.")
            monkeypatch.setattr(vibecop_guardian, "get_project_context", lambda: "Project context:\ngit repo")
            messages, max_tokens = vibecop_guardian.build_messages("rm -rf / # evil", "recursive delete", "s")
            assert max_tokens == 64
            assert messages[0]["role"] == "system"
            assert messages[0]["content"] == "You are a vibecop reviewer."
            user = messages[1]["content"]
            # Command comment is stripped; context and reason are present.
            assert "rm -rf /" in user
            assert "# evil" not in user
            assert "Project context:" in user
            assert "recursive delete" in user

    def test_recent_activity_appears_most_recent_first(self, monkeypatch):
        with _config({"vibecop": {"enabled": True, "activity_window": 3}}):
            monkeypatch.setattr(vibecop_guardian, "load_prompt", lambda: "p")
            vibecop_guardian.record_activity("s", "terminal", "ls", "approve")
            vibecop_guardian.record_activity("s", "terminal", "rm -rf /", "deny")
            result = vibecop_guardian.build_messages("cmd", "desc", "s")
            assert result is not None
            messages, _ = result
            user = messages[1]["content"]
            # Most recent first: deny before approve.
            assert user.index("deny") < user.index("approve")


class TestActivityBuffer:
    def test_record_and_snapshot(self):
        vibecop_guardian.record_activity("s", "terminal", "ls", "approve")
        snap = vibecop_guardian.get_recent_activity("s")
        assert snap == [{"tool": "terminal", "input": "ls", "verdict": "approve"}]

    def test_window_trims_oldest(self, monkeypatch):
        monkeypatch.setattr(vibecop_guardian, "get_activity_window", lambda: 2)
        for i in range(5):
            vibecop_guardian.record_activity("s", "terminal", f"cmd{i}", "approve")
        snap = vibecop_guardian.get_recent_activity("s")
        assert [e["input"] for e in snap] == ["cmd3", "cmd4"]

    def test_clear_session_activity(self):
        vibecop_guardian.record_activity("s", "terminal", "ls", "approve")
        vibecop_guardian.clear_session_activity("s")
        assert vibecop_guardian.get_recent_activity("s") == []
