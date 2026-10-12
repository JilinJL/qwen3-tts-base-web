# Windows TTS Agent

The Windows machine keeps the Qwen3-TTS model and GPU. `tts-agent` is a
separate process from the local FastAPI service and only makes outbound WSS
connections to the gateway.

## Configuration

Add this section to `config.toml` or use the matching
`QWEN3_TTS_AGENT_*` environment variables:

```toml
[tts_agent]
gateway_url = "wss://example.com/v1/worker/ws"
token = "replace-with-a-worker-token"
worker_id = "LINHP"
qwen_url = "http://127.0.0.1:8001"
reconnect_min = 1.0
reconnect_max = 30.0
request_timeout = 3600.0
max_queue = 32
```

Start it after `qwen3-tts-web` is running:

```powershell
./scripts/agent.ps1
```

For local protocol testing without a token only:

```powershell
./scripts/agent.ps1 --allow-anonymous
```

NSSM service registration is provided by `scripts/install-agent.ps1`. The
script expects `nssm.exe` on `PATH` and requires an elevated PowerShell.

## Gateway protocol v1

The agent sends JSON text frames and never writes synthesized audio to disk.
Audio is returned as base64-encoded WAV in the result event.

```text
agent -> gateway: worker.hello
gateway -> agent: job.submit { job: { job_id, text, language, role, emotion } }
agent -> gateway: job.accepted
agent -> gateway: job.running
agent -> gateway: job.result { status, sample_rate, audio_base64 }
gateway -> agent: voice.sync { sync_id, files: [{ path, sha256, content_base64 }] }
agent -> gateway: voice.sync.result
```

`job_id` is idempotent for the lifetime of the agent process. PT paths are
restricted to `pt/<role>/<file>.pt`, SHA-256 is checked before install, and
files are validated before an atomic replace. The queue is deliberately
serial because the underlying model and `inference_lock` are single-device.
