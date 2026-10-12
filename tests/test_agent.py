import base64
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch

import websockets

from qwen3_tts_web.agent import Agent, AgentSettings, normalize_pt_path, sync_voice_files


class AgentPathTests(unittest.TestCase):
    def test_normalize_pt_path_rejects_escape(self):
        with self.assertRaises(ValueError):
            normalize_pt_path("../outside.pt")
        with self.assertRaises(ValueError):
            normalize_pt_path("C:/outside.pt")

    def test_sync_voice_file_validates_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = [
                SimpleNamespace(
                    ref_code=None,
                    ref_spk_embedding=torch.ones(2),
                    x_vector_only_mode=True,
                )
            ]
            source = root / "source.pt"
            torch.save(prompt, source)
            content = source.read_bytes()
            result = sync_voice_files(
                root,
                [
                    {
                        "path": "tester/tester-calm.pt",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                ],
            )
            target = root / "pt" / "tester" / "tester-calm.pt"
            self.assertTrue(target.is_file())
            self.assertEqual(result[0]["bytes"], len(content))


class FakeQwen:
    async def synthesize(self, job):
        return b"RIFF-test-wav", 24000

    async def close(self):
        return None


class AgentProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_wss_job_round_trip_is_serialized(self):
        received = {}

        async def gateway(websocket):
            received["hello"] = json.loads(await websocket.recv())
            await websocket.send(
                json.dumps(
                    {
                        "type": "job.submit",
                        "job": {
                            "job_id": "job-1",
                            "text": "hello",
                            "language": "English",
                            "role": "tester",
                            "emotion": "calm",
                        },
                    }
                )
            )
            events = []
            while True:
                event = json.loads(await websocket.recv())
                events.append(event)
                if event.get("type") == "job.result":
                    received["events"] = events
                    return

        server = await websockets.serve(gateway, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = AgentSettings(
            root=Path(tempfile.gettempdir()),
            gateway_url=f"ws://127.0.0.1:{port}",
            worker_id="test-worker",
        )
        agent = Agent(settings, FakeQwen())
        worker = asyncio.create_task(agent.job_worker())
        try:
            await asyncio.wait_for(agent.connect_once(), timeout=5)
        finally:
            agent._stopping = True
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            server.close()
            await server.wait_closed()

        self.assertEqual(received["hello"]["type"], "worker.hello")
        self.assertEqual(received["hello"]["protocol"], "1")
        self.assertEqual(
            [event["type"] for event in received["events"]],
            ["job.accepted", "job.running", "job.result"],
        )
        self.assertEqual(received["events"][-1]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
