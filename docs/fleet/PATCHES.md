# Fleet Patches — what we carry on top of upstream Hermes

This branch (`fleet`) holds the fleet's divergence from upstream
`NousResearch/hermes-agent` as **one commit per feature**, each sentinel-marked so a
post-upgrade `grep` finds every one of our edits. It replaces the old monolithic
"capture local patches" squash commit.

**Base:** upstream `564aef2946` (2026-09-10). **This branch is the faithful extraction of
what is deployed — it is NOT yet rebased onto current `main`.** Rebasing to current
`main` is the "test run" upgrade (see §Re-apply below); until that lands, this branch is
the canonical record of what we maintain, not a deploy target.

## Patch inventory

| # | Patch | Files | Why it's not upstream |
|---|-------|-------|----------------------|
| 1 | **cross-channel awareness** | `gateway/user_context_tracker.py`, `gateway/session.py`, `hermes_cli/commands.py`, `hermes_cli/config_defaults.py` | Detects session switches and injects cross-channel context gists. Upstream has no `user_context_tracker` module (verified: absent from tree). |
| 2 | **checkpoint trigger** | `gateway/checkpoint_trigger.py`, `tests/gateway/test_checkpoint_trigger.py` | Extracts topics/decisions/questions/artifacts when a session idles into a switch. Absent upstream. |
| 3 | **guardian mode** | `hermes_cli/approvals_cmd.py`, `tools/approval_smart.py`, `tests/tools/test_approval.py`, `hermes_cli/config_defaults.py` | scromp's **vibecop Guardian mode**, ported to Python by us. Upstream has its *own* guardian concept (`tools/approval.py`, `hermes_cli/subcommands/approvals.py`) — a different design, not a replacement for ours. |
| 4 | **relax reboot blocklist** | `tools/approval_detection.py` | Comments out the hardline `shutdown`/`reboot`/`halt`/`poweroff` patterns so Rune can legitimately reboot hosts. |
| 5 | **cross-platform delivery router** | `gateway/platforms/base.py` | `set_session_store()` / `set_delivery_router()` on the base adapter, so the nats-inbox plugin can check active sessions and route outbound messages cross-platform. |
| 6 | **nats extra** | `pyproject.toml`, `uv.lock`, `docs/fleet/POST_UPGRADE.md` | Declares `nats-py` as a `nats` extra (folded into `all`) so `uv sync` keeps the nats-inbox plugin's dependency. Absent upstream. |

## Dropped (already upstream — do not re-apply)

- **delegation per-task model/provider override** — upstream `tools/delegate_tool.py`
  already carries `override_provider` / `override_model`. Our PR #107717 was closed as a
  duplicate of #41843, which landed.
- **title-gen fix (romar#317, "clomp has a json problem")** — upstream `agent/title_generator.py`
  independently fixed the same bug class via `_is_truncated_structured_output` (#83903),
  `_extract_json_title`, and `reasoning_config={"enabled": False}` (#91927).

## Sentinel markers

Sentinel coverage is **currently partial** — a gap inherited from the original capture
commit, to be closed during the current-`main` port:

- **cross-channel awareness** is sentinel-marked (`# === CROSS-CHANNEL START/END ===` in
  `session.py`, `commands.py`, and the `cross_channel` config block).
- The remaining patches (checkpoint, guardian, reboot blocklist, delivery router, nats)
  are **not** yet marked. When re-applying after an upstream rebase, wrap each edit in a
  sentinel pair (`# === <FEATURE> START/END ===`) so `grep` stays the inventory.

`grep -rn "=== .* START" --include="*.py" gateway/ hermes_cli/ tools/` lists every marked
site. The sentinel is part of the patch, not decoration.

## Re-apply procedure (upgrade checklist)

1. Rebase this branch onto current upstream `main`: `git fetch origin && git rebase origin/main`.
2. For each patch commit, resolve conflicts file-by-file. New files (checkpoint_trigger,
   user_context_tracker, approvals_cmd) port cleanly; the hook files (`session.py`,
   `base.py`, `commands.py`, `config_defaults.py`, `approval_smart.py`) have been refactored
   upstream (facade → `*_sibling` split), so re-locate the hook at its new call site rather
   than forcing the old line.
3. Re-run `uv lock` (or re-apply the nats lock delta) so `uv sync --extra all` keeps nats-py.
4. Verify sentinels survive: `grep -rn "CROSS-CHANNEL START" gateway/` → expect ≥ 2.
5. Test on one host before fleet rollout: guardian approvals, cross-channel gist injection,
   checkpoint trigger, nats-inbox delivery.
