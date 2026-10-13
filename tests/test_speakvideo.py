import base64
import asyncio
import hashlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

import torch
import websockets

from scripts.speakvideo.base_agent import AgentSettings
from scripts.speakvideo.metrics import collect_metrics
from scripts.speakvideo.interface_docs import SCHEMA, build_interface_document
from scripts.speakvideo.speakvideo_agent import SpeakVideoAgent
from scripts.speakvideo.voice_library import VoiceLibrary


def prompt_bytes() -> bytes:
    stream = io.BytesIO()
    torch.save(
        [
            SimpleNamespace(
                ref_code=None,
                ref_spk_embedding=torch.ones(2),
                x_vector_only_mode=True,
            )
        ],
        stream,
    )
    return stream.getvalue()


def wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setparams((1, 2, 24000, 0, "NONE", "none"))
        writer.writeframes(b"\x00\x00" * 32)
    return stream.getvalue()


class VoiceLibraryTests(unittest.TestCase):
    def test_manage_import_edit_delete_restore_and_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            library = VoiceLibrary(Path(directory))
            content = prompt_bytes()
            encoded = base64.b64encode(content).decode("ascii")
            asset = library.manage(
                {
                    "action": "import",
                    "role": "tester",
                    "emotion": "calm",
                    "content_base64": encoded,
                    "ref_text": "hello",
                    "notes": "fixture",
                    "origin": "test",
                }
            )
            self.assertEqual(asset["role"], "tester")
            self.assertEqual(asset["sha256"], hashlib.sha256(content).hexdigest())

            created = library.record_created(
                {"role": "tester", "emotion": "calm", "ref_text": "hello"},
                base64.b64encode(wav_bytes()).decode("ascii"),
            )
            self.assertTrue(created["has_reference"])
            edited = library.manage(
                {
                    "action": "edit",
                    "role": "tester",
                    "emotion": "calm",
                    "new_role": "renamed",
                    "new_emotion": "quiet",
                    "sha256": created["sha256"],
                }
            )
            self.assertEqual(edited["filename"], "renamed_quiet.pt")
            reference = library.manage(
                {
                    "action": "reference",
                    "role": "renamed",
                    "emotion": "quiet",
                    "sha256": edited["sha256"],
                }
            )
            self.assertTrue(base64.b64decode(reference["content_base64"]).startswith(b"RIFF"))
            deleted = library.manage(
                {
                    "action": "delete",
                    "role": "renamed",
                    "emotion": "quiet",
                    "sha256": edited["sha256"],
                }
            )
            self.assertTrue(deleted["deleted"])
            self.assertEqual(library.list_assets()["assets"], [])
            restored = library.manage({"action": "restore", "id": deleted["trash_id"]})
            self.assertTrue(restored["restored"])
            self.assertEqual(len(library.list_assets()["assets"]), 1)


class MetricsTests(unittest.TestCase):
    def test_metrics_shape_does_not_expose_job_text(self):
        result = collect_metrics(
            queue_depth=2,
            current_job_id="j_abc",
            logs=["job.running job_id=j_abc"],
        )
        self.assertEqual(result["queue_depth"], 2)
        self.assertEqual(result["current_job_id"], "j_abc")
        self.assertIn("gpus", result)
        self.assertNotIn("secret text", str(result))


class NoopQwen:
    async def synthesize(self, job):
        raise AssertionError("no job expected")

    async def close(self):
        return None


class ProtocolExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_rpc_returns_machine_readable_interface_docs(self):
        settings = AgentSettings(
            root=Path(tempfile.mkdtemp()),
            gateway_url="wss://example.invalid/v1/worker/ws",
            token="t" * 32,
            worker_id="LINHP",
        )
        agent = SpeakVideoAgent(settings, NoopQwen())
        try:
            await agent.rpc({"type": "worker.capabilities", "request_id": "cap-1"})
            result = await agent.next_event()
        finally:
            agent.journal.close()
        self.assertEqual(result["type"], "rpc.result")
        self.assertEqual(result["request_id"], "cap-1")
        self.assertEqual(result["result"]["schema"], SCHEMA)
        self.assertTrue(result["result"]["worker_id"], "worker id is required")
        self.assertNotIn("token", json.dumps(result["result"]).lower())
        rpc_types = {item["type"] for item in result["result"]["rpc"]}
        self.assertIn("voice.manage", rpc_types)
        self.assertIn("worker.capabilities", rpc_types)
        self.assertEqual(
            set(result["result"]["voice_manage_actions"]),
            {"list", "import", "edit", "delete", "reference", "trash", "restore"},
        )

    async def test_hello_advertises_durable_rpc_capabilities(self):
        received = {}

        async def gateway(websocket):
            received["hello"] = json.loads(await websocket.recv())
            await websocket.send(json.dumps({"type": "worker.ping"}))
            received["pong"] = json.loads(await websocket.recv())

        server = await websockets.serve(gateway, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = AgentSettings(
            root=Path(tempfile.mkdtemp()),
            gateway_url=f"ws://127.0.0.1:{port}",
            token="t" * 32,
            worker_id="LINHP",
        )
        agent = SpeakVideoAgent(settings, NoopQwen())
        try:
            await asyncio.wait_for(agent.connect_once(), timeout=5)
        finally:
            agent.journal.close()
            server.close()
            await server.wait_closed()

        self.assertEqual(received["hello"]["type"], "worker.hello")
        self.assertTrue(received["hello"]["capabilities"]["durable_jobs"])
        self.assertTrue(received["hello"]["capabilities"]["rpc"])
        self.assertTrue(received["hello"]["capabilities"]["correlated_acks"])
        self.assertTrue(received["hello"]["capabilities"]["interface_docs"])
        self.assertEqual(received["pong"]["type"], "worker.pong")

    async def test_metrics_rpc_preserves_request_id(self):
        settings = AgentSettings(
            root=Path(tempfile.mkdtemp()),
            gateway_url="wss://example.invalid/v1/worker/ws",
            token="t" * 32,
            worker_id="LINHP",
        )
        agent = SpeakVideoAgent(settings, NoopQwen())
        try:
            await agent.rpc({"type": "worker.metrics", "request_id": "metrics-1"})
            result = await agent.next_event()
        finally:
            agent.journal.close()
        self.assertEqual(result["type"], "rpc.result")
        self.assertEqual(result["request_id"], "metrics-1")
        self.assertIn("gpus", result["result"])


if __name__ == "__main__":
    unittest.main()
