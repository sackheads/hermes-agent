"""Disabling reasoning must actually stop Gemini thinking (#91927, romar#332).

``includeThoughts: False`` only hides thought parts; the model still reasons
internally and bills thought tokens against maxOutputTokens, starving small
budgets (title generation's 64 tokens).
- Gemini 2.5 uses ``thinkingBudget: 0``.
- Gemini 3 series (and moving aliases) deprecated ``thinking_budget`` (hard-errors
  with 400 INVALID_ARGUMENT on upcoming models); it uses ``thinkingLevel: "minimal"``.
"""

import pytest

from agent.transports.chat_completions import (
    _build_gemini_thinking_config,
    _snake_case_gemini_thinking_config,
)


@pytest.mark.parametrize(
    "model,expect_budget_zero,expected_thinking_level",
    [
        ("gemini-2.5-flash", True, None),
        ("gemini-3.6-flash", False, "minimal"),
        ("gemini-3.8-flash", False, "low"),
        ("gemini-3.1-pro", False, "low"),
        ("gemini-flash-latest", False, "low"),
        ("gemini-pro-latest", False, "low"),
        ("gemini-1.5-flash", False, None),  # pre-2.5: thinkingBudget undocumented
    ],
)
def test_disabled_reasoning_minimizes_or_zeroes_thinking_where_supported(
    model, expect_budget_zero, expected_thinking_level
):
    for reasoning in ({"enabled": False}, {"effort": "none"}):
        config = _build_gemini_thinking_config(model, reasoning)
        assert config is not None
        assert config.get("includeThoughts") is False
        assert (config.get("thinkingBudget") == 0) is expect_budget_zero
        assert config.get("thinkingLevel") == expected_thinking_level
        if not expect_budget_zero:
            assert "thinkingBudget" not in config
        if expected_thinking_level is None:
            assert "thinkingLevel" not in config


def test_enabled_reasoning_never_zeroes_budget_and_non_gemini_gets_nothing():
    # Enabled reasoning must not be silently strangled by a zero budget.
    for reasoning in ({"enabled": True}, {"effort": "medium"}):
        config = _build_gemini_thinking_config("gemini-2.5-flash", reasoning)
        assert config is not None
        assert config.get("includeThoughts") is True
        assert "thinkingBudget" not in config
        assert "thinkingLevel" not in config

    # Gemini 3 maps effort to thinkingLevel.
    cfg_flash = _build_gemini_thinking_config("gemini-3.6-flash", {"effort": "low"})
    assert cfg_flash == {"includeThoughts": True, "thinkingLevel": "low"}

    cfg_pro = _build_gemini_thinking_config("gemini-3.1-pro", {"effort": "high"})
    assert cfg_pro == {"includeThoughts": True, "thinkingLevel": "high"}

    # Non-Gemini models on the same provider 400 on the field entirely (#17426).
    assert _build_gemini_thinking_config("gpt-4o", {"enabled": False}) is None
    assert _build_gemini_thinking_config("gemma-2b", {"enabled": False}) is None


def test_snake_case_translation_carries_thinking_level_and_budget():
    translated = _snake_case_gemini_thinking_config({"includeThoughts": False, "thinkingLevel": "minimal"})
    assert translated == {"include_thoughts": False, "thinking_level": "minimal"}
    translated = _snake_case_gemini_thinking_config({"includeThoughts": False, "thinkingBudget": 0})
    assert translated == {"include_thoughts": False, "thinking_budget": 0}
    translated = _snake_case_gemini_thinking_config({"includeThoughts": False})
    assert translated == {"include_thoughts": False}
