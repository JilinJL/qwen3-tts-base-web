# Windows SpeakVideo Agent

The Windows machine keeps the Qwen3-TTS model and GPU. The production worker
is `scripts/speakvideo/speakvideo_agent.py`; it is separate from the local
FastAPI service and only makes outbound WSS connections to the gateway. The
legacy `tts-agent` entry point remains available for protocol-v1 compatibility.

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

For the upgraded SpeakVideo contract, start Qwen and the worker with the
scheduled-task installer (run PowerShell as Administrator):

```powershell
./scripts/speakvideo/install.ps1 -Start
```

For a foreground worker run, after `qwen3-tts-web` is running:

```powershell
./scripts/speakvideo/run.ps1 -Mode agent
```

The worker writes only job metadata to `.speakvideo/jobs.sqlite3`. Generated
WAV data stays in memory; PT metadata, reference WAV files, and soft-delete
archives are stored under `.speakvideo/voice-trash/` as required by the
Windows integration contract.

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

The upgraded contract uses durable IDs matching `j_<64 lowercase hex>` and
persists job metadata in SQLite before acknowledging acceptance. PT paths are
restricted to `pt/<role>/<role>_<emotion>.pt`, SHA-256 is checked again before
inference, and files are validated before an atomic replace. The queue is
deliberately serial because the underlying model and `inference_lock` are
single-device.

The worker also implements `worker.health`, `worker.metrics`, `job.query`,
`voice.list`, `voice.download`, `voice.create`, `voice.design`,
`voice.delete`, and `voice.manage` RPCs. See the Mac integration contract for
the full request and response schema.
