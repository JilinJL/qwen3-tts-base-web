"""Machine-readable SpeakVideo Windows worker interface documentation."""

from __future__ import annotations

from copy import deepcopy


SCHEMA = "speakvideo.windows-agent.interface-document"
SCHEMA_VERSION = "1.0"
PROTOCOL_VERSION = "1"


def _rpc(type_name: str, summary: str, request: dict, response: dict) -> dict:
    return {
        "type": type_name,
        "kind": "rpc",
        "summary": summary,
        "request": request,
        "response": response,
        "implemented": True,
    }


def build_interface_document(worker_id: str) -> dict:
    """Return a token-free document that Mac can use for capability discovery."""

    document = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL_VERSION,
        "worker_id": worker_id,
        "transport": {
            "kind": "websocket",
            "direction": "gateway_to_worker over outbound WSS",
            "hello": {
                "type": "worker.hello",
                "protocol": PROTOCOL_VERSION,
                "worker_id": worker_id,
                "capabilities": {
                    "tts": True,
                    "voice_sync": True,
                    "audio_encoding": "base64-wav",
                    "durable_jobs": True,
                    "rpc": True,
                    "correlated_acks": True,
                    "interface_docs": True,
                },
            },
            "rpc_correlation": {
                "request_field": "request_id",
                "response_type": "rpc.result",
                "response_request_id": "same as request_id",
                "error_field": "rpc_error",
            },
        },
        "rpc": [
            _rpc(
                "worker.capabilities",
                "Return this interface document.",
                {"type": "worker.capabilities", "request_id": "string"},
                {"type": "rpc.result", "result": "interface document"},
            ),
            _rpc(
                "worker.health",
                "Return Qwen readiness and worker capabilities.",
                {
                    "type": "worker.health",
                    "request_id": "string",
                    "include_docs": "optional boolean, default true",
                },
                {
                    "type": "rpc.result",
                    "ready": "boolean",
                    "device": "string",
                    "model": "string",
                    "worker_id": "string",
                    "capabilities": "object",
                    "interface_docs": "interface document when include_docs is true",
                },
            ),
            _rpc(
                "worker.metrics",
                "Return CPU, memory, GPU, queue, and recent event metrics.",
                {"type": "worker.metrics", "request_id": "string"},
                {"type": "rpc.result", "result": "metrics object"},
            ),
            _rpc(
                "job.query",
                "Query a durable job and optionally return cached audio.",
                {
                    "type": "job.query",
                    "request_id": "string",
                    "job_id": "j_<64 lowercase hex characters>",
                    "include_audio": "optional boolean",
                },
                {
                    "type": "rpc.result",
                    "job_id": "string",
                    "status": "missing|queued|running|succeeded|failed|cancelled",
                    "audio_base64": "only for succeeded jobs with include_audio=true",
                },
            ),
            _rpc(
                "voice.list",
                "List PT assets installed on Windows.",
                {"type": "voice.list", "request_id": "string"},
                {"type": "rpc.result", "result": "Qwen PT list object"},
            ),
            _rpc(
                "voice.download",
                "Download one PT asset with its SHA-256.",
                {
                    "type": "voice.download",
                    "request_id": "string",
                    "role": "string",
                    "emotion": "string",
                },
                {
                    "type": "rpc.result",
                    "content_base64": "PT bytes",
                    "sha256": "64 lowercase hex characters",
                },
            ),
            _rpc(
                "voice.create",
                "Create a PT asset from in-memory reference audio.",
                {
                    "type": "voice.create",
                    "request_id": "string",
                    "metadata": {
                        "role": "string",
                        "emotion": "string",
                        "ref_text": "non-empty string",
                        "notes": "optional string",
                        "overwrite": "boolean",
                    },
                    "content_base64": "reference audio, <= 32 MiB",
                },
                {"type": "rpc.result", "result": "voice asset detail"},
            ),
            _rpc(
                "voice.design",
                "Generate a WAV from a voice design instruction in memory.",
                {
                    "type": "voice.design",
                    "request_id": "string",
                    "payload": "Qwen VoiceDesignRequest fields",
                },
                {
                    "type": "rpc.result",
                    "audio_base64": "WAV bytes, <= 64 MiB",
                },
            ),
            _rpc(
                "voice.delete",
                "Soft-delete a PT asset after SHA-256 verification.",
                {
                    "type": "voice.delete",
                    "request_id": "string",
                    "role": "string",
                    "emotion": "string",
                    "sha256": "current PT SHA-256",
                },
                {"type": "rpc.result", "result": "deleted asset detail"},
            ),
            _rpc(
                "voice.manage",
                "Manage the Windows PT library, including edit and restore.",
                {
                    "type": "voice.manage",
                    "request_id": "string",
                    "payload": {
                        "action": "list|import|edit|delete|reference|trash|restore",
                        "role": "required for import/edit/delete/reference",
                        "emotion": "required for import/edit/delete/reference",
                        "sha256": "required for edit/delete/reference",
                    },
                },
                {"type": "rpc.result", "result": "action-specific object"},
            ),
        ],
        "events": [
            {
                "type": "worker.ping",
                "response": "worker.pong with worker_id and ts",
                "implemented": True,
            },
            {
                "type": "job.submit",
                "response": "job.accepted, job.rejected, job.running, job.result",
                "implemented": True,
            },
            {
                "type": "job.cancel",
                "response": "job.result with status=cancelled",
                "implemented": True,
            },
            {
                "type": "voice.sync",
                "response": "voice.sync.result",
                "implemented": True,
            },
        ],
        "voice_manage_actions": {
            "list": "List assets and metadata.",
            "import": "Install a supplied PT after validation; optional overwrite archives the old asset.",
            "edit": "Rename or update metadata after SHA-256 verification.",
            "delete": "Soft-delete after SHA-256 verification.",
            "reference": "Download the retained reference WAV.",
            "trash": "List soft-deleted assets.",
            "restore": "Restore a soft-deleted asset without overwriting a live asset.",
        },
        "limits": {
            "pt_bytes": 32 * 1024 * 1024,
            "reference_bytes": 32 * 1024 * 1024,
            "design_audio_bytes": 64 * 1024 * 1024,
            "job_text_characters": 10000,
            "job_id_pattern": "^j_[a-f0-9]{64}$",
            "audio_transport": "base64 WAV in memory; never written as a generated intermediate",
        },
        "internal_qwen_http": [
            {"method": "GET", "path": "/api/health"},
            {"method": "GET", "path": "/api/pt/list"},
            {"method": "POST", "path": "/api/pt/create"},
            {"method": "POST", "path": "/api/tts"},
            {"method": "POST", "path": "/api/voice-design"},
        ],
    }
    return deepcopy(document)
