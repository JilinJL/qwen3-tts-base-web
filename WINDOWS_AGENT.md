# Windows SpeakVideo Agent

The Windows machine keeps the Qwen3-TTS model and GPU. The production worker
is `scripts/speakvideo/speakvideo_agent.py`; it is separate from the local
FastAPI service and only makes outbound WSS connections to the remote service. The
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

For normal operation, use the elevated convenience scripts. They register the
startup tasks when needed, avoid starting a second healthy Qwen process, and
wait for child processes to release their locks during shutdown:

```powershell
./scripts/speakvideo/start.ps1              # start Qwen and the Agent
./scripts/speakvideo/start.ps1 -Mode agent  # start only the Agent
./scripts/speakvideo/stop.ps1               # stop both services
./scripts/speakvideo/stop.ps1 -Mode agent   # stop only the Agent
```

Both scripts request administrator permission through UAC when necessary.
The Agent launcher derives the remote WSS host from `config.toml` and adds it to
`NO_PROXY`, so the Windows system proxy cannot stall the outbound WSS session.

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

## Worker protocol v1

The agent sends JSON text frames and never writes synthesized audio to disk.
Audio is returned as base64-encoded WAV in the result event.

```text
agent -> server: worker.hello
server -> agent: job.submit { job: { job_id, text, language, role, emotion } }
agent -> server: job.accepted
agent -> server: job.running
agent -> server: job.result { status, sample_rate, audio_base64 }
server -> agent: voice.sync { sync_id, files: [{ path, sha256, content_base64 }] }
agent -> server: voice.sync.result
```

The upgraded contract uses durable IDs matching `j_<64 lowercase hex>` and
persists job metadata in SQLite before acknowledging acceptance. PT paths are
restricted to `pt/<role>/<role>_<emotion>.pt`, SHA-256 is checked again before
inference, and files are validated before an atomic replace. The queue is
deliberately serial because the underlying model and `inference_lock` are
single-device.

The worker also implements `worker.health`, `worker.metrics`, `job.query`,
`voice.list`, `voice.download`, `voice.create`, `voice.design`,
`voice.delete`, `voice.manage`, and `worker.capabilities` RPCs. Clients should query
`worker.capabilities` after every Worker connection instead of hard-coding an
older feature list. The same machine-readable document is included by
`worker.health` unless the request contains `include_docs: false`.

Voice deletion has two explicit modes:

- `voice.manage` with `payload.action = "delete"` soft-deletes the PT, metadata,
  and reference WAV into `.speakvideo/voice-trash/`.
- `voice.manage` with `payload.action = "delete_permanent"` verifies the
  current PT SHA-256 and permanently removes the PT and its `.json`/`.wav`
  sidecars without creating a trash entry.
- The compatibility `voice.delete` RPC remains soft-delete by default. It
  accepts `permanent: true` when an older client needs the permanent behavior.

Both permanent forms require `role`, `emotion`, and the current `sha256` when
the PT exists. A stale SHA is rejected so a delayed client request cannot delete
a new asset that reused the same role and emotion.

The capabilities document is token-free and contains the transport handshake,
all worker RPCs and events, voice-management actions, payload limits, and the
internal Qwen routes used by the worker. The remote service must forward the generic
RPC request and response with the original `request_id`; no Windows HTTP port
needs to be exposed.

Example query over the existing Worker WebSocket:

```json
{"type":"worker.capabilities","request_id":"cap-<unique-id>"}
```

The response is a `rpc.result` with the same `request_id` and the document in
`result`. A lightweight health query can use
`{"type":"worker.health","request_id":"health-1","include_docs":false}`.
