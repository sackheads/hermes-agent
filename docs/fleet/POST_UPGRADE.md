# Fleet Post-Upgrade Runbook

The `fleet-port` upgrade (Sept-10 base → current upstream `main`) is **not** just
reapplying the six fleet patches. It also brings two upstream changes that change
how a host is installed and upgraded:

1. **Python 3.11 → 3.14** (upstream `7f1ddc70ce8`, "move first-party runtime to 3.14").
2. **A package-management / isolated-runtime model** — Hermes provisions its own
   Python and dependencies on first run instead of living in a hand-built `venv`.

## The old `uv sync` recipe is obsolete

The previous runbook (and the pre-port fleet) maintained a single `venv` at
`~/.hermes/hermes-agent/venv` with:

```bash
uv sync --extra all --extra exa --extra anthropic --extra messaging --extra edge-tts
```

That recipe applied to the Sept-10 base. On current `main` it is **redundant and
misleading**: the gateway no longer runs from that `venv`. The current model
provisions its own isolated runtime:

- Python: `~/.hermes/tools/python-3.14.x-linux-x64/bin/python3` (uv-managed).
- Dependencies: `~/.hermes/installs/<hash>/environments/<hash>/`.
- On first run after a code change, `hermes` auto-completes a "source update":
  installs Python deps, Node deps, builds the TUI and web UI, refreshes the model
  catalog, syncs bundled skills, and migrates `config.yaml`.

So after a `git checkout`/`pull`, the runtime is provisioned by the next `hermes`
invocation or gateway restart — **not** by `uv sync`.

## Upgrade procedure (fleet-port)

```bash
# 1. Backup (pre-upgrade-backup skill / hermes-pre-upgrade-backup.py)
python3 /shared/agents/common/scripts/hermes-pre-upgrade-backup.py

# 2. Swap the code
cd ~/.hermes/hermes-agent
git checkout fleet-port        # or the target branch/tag

# 3. Restart the gateway — this triggers auto-provisioning (deps + Python + UI
#    build + config migration). First boot after a big jump can take minutes.
systemctl --user restart hermes-gateway.service
```

Rollback: `git checkout fleet-rebase` (or the prior branch) + restart.

## Verify (do all of these)

```bash
# Gateway up and stable, running the expected code
systemctl --user is-active hermes-gateway.service
python3 -c "import json;d=json.load(open('$HOME/.hermes/gateway_state.json'));print(d['code_sha'], d['code_version'])"
#   code_sha must match the checked-out commit.

# Fleet modules import (sentinels intact)
cd ~/.hermes/hermes-agent
grep -rn "CROSS-CHANNEL START" gateway/ | wc -l               # expect >= 2
grep -rn "VIBECOP START" tools/approval_smart.py | wc -l      # expect 2
venv/bin/python -c "import gateway.user_context_tracker, gateway.checkpoint_trigger, tools.vibecop_guardian"

# NATS inbox consumer is push-bound
venv/bin/python - <<'PY'
import asyncio, nats
async def main():
    nc = await nats.connect('nats://10.3.10.55:4222', connect_timeout=5)
    js = nc.jetstream()
    ci = await js.consumer_info('agent-coordination', 'nats-inbox-rune')
    print('push_bound=', ci.push_bound, 'pending=', ci.num_pending)
    await nc.close()
asyncio.run(main())
PY

# vibecop CLI
hermes vibecop status

# Install health
hermes doctor
```

## Pitfalls (hit during the diffuser one-host test, 2026-10-05)

- **Stale `SSL_CERT_FILE`** — a shell/agent env carrying
  `.../venv/lib/python3.11/site-packages/certifi/cacert.pem` breaks the
  auto-provisioning download with `invalid peer certificate: UnknownIssuer`,
  because the 3.11 path no longer exists after the 3.14 bump. Unset it (or point
  it at the new certifi path) before the first `hermes` invocation. The systemd
  unit does **not** set it, so a gateway restart is unaffected — only interactive
  shells that inherited it.
- **"a source update is unfinished; run `hermes update` to finish it"** — prints on
  startup until the update receipt is written. Harmless; `hermes update` from a host
  shell completes it (and restarts the gateway again).
- **`hermes-memory` plugin** lives at `plugins/memory/hermes_memory/` as an
  *untracked* directory (deployed separately from `bnaylor/hermes-memory`). A
  `git checkout` never touches it; its only runtime dep is PyYAML (core).
- **`homeassistant` plugin not auto-installed** — it requires `hermes >=0.21.5`,
  but `fleet-port` is `0.21.1`. `hermes plugins install homeassistant` if needed.

## Why this happens

The current `hermes` uses a PM (package-manager) / isolated-runtime model: the
launcher provisions a dedicated Python + dependency set under `~/.hermes/` and the
gateway runs from `~/.hermes/tools/python-3.14.x`. The `venv/` directory in the
source tree is now only a dev/CLI convenience, not the gateway runtime.
