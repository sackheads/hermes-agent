# Fleet Patches — what we carry on top of upstream Hermes

This branch (`fleet-port`) is the fleet's divergence from upstream
`NousResearch/hermes-agent`, **ported onto current upstream `main`** — one commit per
feature, sentinel-marked where it touches a hook file, so a post-upgrade `grep`
finds every one of our edits. It is the deploy/test target. The faithful
extraction of the *original* deployed state (Sept-10 base) lives on the
`sackheads/hermes-agent:fleet-patches` branch alongside the re-apply procedure.

**Base:** upstream `7b362884d88` ("Merge pull request #133062", 2026-10-05).

## Patch inventory

| # | Patch | Files | Why it's not upstream |
|---|-------|-------|----------------------|
| 1 | **cross-channel awareness** | `gateway/user_context_tracker.py`, `gateway/session.py`, `hermes_cli/commands.py`, `hermes_cli/config_defaults.py` | Detects session switches and injects cross-channel context gists. Upstream has no `user_context_tracker` module. |
| 2 | **checkpoint trigger** | `gateway/checkpoint_trigger.py`, `tests/gateway/test_checkpoint_trigger.py` | Extracts topics/decisions/questions/artifacts when a session idles into a switch. Absent upstream. **UNWIRED** — the module ships but has no call site (tracked as `sackheads/romar#326`). |
| 3 | **vibecop Guardian mode** | `tools/vibecop_guardian.py`, `hermes_cli/vibecop_cmd.py`, `hermes_cli/subcommands/vibecop.py`, `hermes_cli/main.py`, `hermes_cli/config_defaults.py`, `tools/approval_smart.py` | scromp's **vibecop Guardian mode** (project-aware smart approvals), ported to Python. Upstream has its *own* guardian concept (`tools/approval_smart.py`, `hermes_cli/subcommands/approvals.py`) — a different design, not a replacement. Ours lives in a standalone module (`vibecop_guardian.py`) so it never entangles with upstream's rewrite, exposes `hermes vibecop {status,init,refine}`, and reads `approvals.vibecop.*`. |
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

`grep -rn "=== .* START" --include="*.py" gateway/ hermes_cli/ tools/` lists every
marked site. The sentinel is part of the patch, not decoration.

- **cross-channel awareness** — `# === CROSS-CHANNEL START/END ===` in `session.py`,
  `commands.py`, and the `cross_channel` config block.
- **vibecop Guardian mode** — `# === VIBECOP START/END ===` at the two hooks in
  `tools/approval_smart.py` (`_smart_approve` prompt selection, `_smart_verdict`
  activity recording). The bulk of the logic is isolated in `tools/vibecop_guardian.py`.
- Remaining patches (checkpoint, reboot blocklist, delivery router, nats) are **not yet**
  sentinel-marked — a gap inherited from the original capture commit.

## Port notes (what changed vs the Sept-10 extraction)

The port re-located hooks to their upstream-refactored call sites (facade →
`*_sibling` split) and fixed two defects in the original guardian extraction:

1. **`record_activity` was dead** — defined but never called, so the recent-verdict
   buffer never filled. Now wired into `_smart_verdict` (best-effort, never blocks the verdict).
2. **The CLI was unreachable** — `approvals_cmd.py` shipped but was never registered in
   `main.py`. The renamed `hermes vibecop` command is now wired through
   `hermes_cli/subcommands/vibecop.py`.

## Re-apply / upgrade checklist

1. Rebase this branch onto current upstream `main`; re-locate each hook at its (possibly
   re-refactored) call site rather than forcing the old line.
2. `uv sync --extra all --extra exa --extra anthropic --extra messaging --extra edge-tts`
   (see `POST_UPGRADE.md`) — bare `uv sync` drops the optional extras.
3. Verify sentinels survive: `grep -rn "CROSS-CHANNEL START" gateway/` → expect ≥ 2;
   `grep -rn "VIBECOP START" tools/approval_smart.py` → expect 2.
4. Test on one host before fleet rollout: vibecop approvals, cross-channel gist injection,
   checkpoint trigger, nats-inbox delivery (`POST_UPGRADE.md`).
