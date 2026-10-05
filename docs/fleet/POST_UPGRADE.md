# Fleet Post-Upgrade Runbook

Every code upgrade of the fleet Hermes install (manual `git checkout`/`pull`, or
`hermes update`) requires a dependency reinstall. If you run bare `uv sync`, the
venv is reset to **core dependencies only** and silently removes the optional
extras and ad-hoc plugin deps the fleet relies on.

## What breaks on a bare `uv sync`

| Feature | Missing dep | Declared in `pyproject.toml`? |
|---|---|---|
| MCP servers (k8s-lookout, sharpei) | `mcp` + `httpx2` | yes — `mcp` extra (also in `all`) |
| Web search / extract | `exa-py` | yes — `exa` extra |
| Anthropic provider | `anthropic` | yes — `anthropic` extra |
| Telegram / Slack adapters | `telegram` / `slack` | yes — `messaging` extra |
| TTS | `edge-tts` | yes — `edge-tts` extra |
| NATS inbox | `nats-py` | yes — `nats` extra (in `all`) |

Symptoms: `nats-middle-health` cron fails with
`Platform 'NATS Inbox' requirements not met (pip install nats-py)`; `hermes mcp test`
fails with `requires the 'mcp' Python SDK, but it is not installed`.

## The correct reinstall

```bash
cd ~/.hermes/hermes-agent
uv sync --extra all --extra exa --extra anthropic --extra messaging --extra edge-tts
```

`nats-py` is covered by `--extra all` (it's in the `nats` extra). Add `--extra <name>` for
any other lazy/optional feature the fleet enables.

## Verify (do all of these)

```bash
# Gateway up and stable (no crash-loop)
systemctl --user is-active hermes-gateway.service

# NATS inbox consumer is push-bound
~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import asyncio, nats
async def main():
    nc = await nats.connect('nats://10.3.10.55:4222', connect_timeout=5)
    js = nc.jetstream()
    ci = await js.consumer_info('agent-coordination', 'nats-inbox-rune')
    print('push_bound=', ci.push_bound, 'pending=', ci.num_pending)
    await nc.close()
asyncio.run(main())
PY

# MCP servers connect + enumerate tools
hermes mcp test k8s-lookout
hermes mcp test sharpei

# Install health
hermes doctor
```

## Why this happens

`uv sync` makes the venv match `pyproject.toml` + `uv.lock` **exactly**. Anything not
declared there — optional extras you didn't name, and any `uv pip install`-ed ad-hoc
package — is removed. `hermes update` requests the right extras for you; a manual
`git checkout` + bare `uv sync` does not.

## Root fix (done)

`nats-py` is declared as a `nats` extra in `pyproject.toml` (and included in the `all`
extra), so `uv sync --extra all` keeps it. No manual `uv pip install nats-py` step is
needed anymore.
