"""Real loopback/decoder tests with a sentinel model: no GPU or model load.

Run: python -m unittest discover -s Tests -p 'test_*.py'
Requires the runtime's NumPy and PyAV packages.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
from ml.inference import InferenceScheduler
from ml.stt_server import STTServer, MultipartUpload, MAX_BODY, MODEL_ID, HTTPError, token_file
from ml.stt_audio import decode_audio, audio_chunks, join_text, AudioError


def wav_bytes(seconds=0.2, rate=44100, channels=2):
    data = io.BytesIO()
    with wave.open(data, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(bytes(int(seconds * rate) * channels * 2))
    return data.getvalue()


def multipart(audio=None, **fields):
    boundary = "ScribeKitt-test-boundary"
    body = b""
    for name, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n').encode()
    body += (wav_bytes() if audio is None else audio) + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = InferenceScheduler(idle_seconds=0.05)

    def tearDown(self):
        self.scheduler.close()

    def test_dictation_bypasses_api_and_idle_timer_restarts(self):
        started, release = threading.Event(), threading.Event()
        order = []
        def chunk():
            started.set()
            release.wait(2)
            order.append("chunk1")
        first = self.scheduler.submit(chunk, api=True)
        self.assertTrue(started.wait(1))
        second = self.scheduler.submit(lambda: order.append("chunk2"), api=True)
        spoken = self.scheduler.submit(lambda: order.append("dictation"))
        release.set()
        spoken.result(1)
        finished = time.monotonic()
        second.result(1)
        first.result(1)
        self.assertEqual(order, ["chunk1", "dictation", "chunk2"])
        self.assertGreaterEqual(time.monotonic() - finished, 0.04)

    def test_recording_reservation_and_cancellation_release_audio(self):
        self.scheduler.set_recording(True)
        for _ in range(100):
            job = self.scheduler.submit(lambda: self.fail("Cancelled job ran"), api=True)
            job.cancel()
        self.assertEqual(len(self.scheduler.api), 0)
        api = self.scheduler.submit(lambda: "api", api=True)
        self.assertEqual(self.scheduler.submit(lambda: "dictation").result(1), "dictation")
        self.assertFalse(api.done())
        self.scheduler.set_recording(False)
        self.assertEqual(api.result(1)[0], "api")

    def test_close_does_not_wait_for_current_chunk(self):
        started, release = threading.Event(), threading.Event()
        def chunk():
            started.set()
            release.wait(2)
        self.scheduler.submit(chunk, api=True)
        self.assertTrue(started.wait(1))
        pending = self.scheduler.submit(lambda: None, api=True)
        begin = time.monotonic()
        self.scheduler.close()
        self.assertLess(time.monotonic() - begin, 0.1)
        self.assertTrue(pending.cancelled())
        release.set()


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scheduler = InferenceScheduler(idle_seconds=0.01)
        self.model = object()
        self.seen = []
        def infer(model, samples):
            self.seen.append((model, len(samples), threading.current_thread().name))
            return "test transcript"
        self.server = STTServer(self.scheduler, self.tmp.name, version="test", model_getter=lambda: self.model, infer=infer)
        self.status = self.server.configure(True, 0)
        self.assertIsNone(self.status["error"])
        self.port = self.status["port"]
        self.token = (Path(self.tmp.name) / "stt_server.token").read_text().strip()

    def tearDown(self):
        self.server.close()
        self.scheduler.close()
        self.tmp.cleanup()

    def request(self, method="GET", path="/healthz", body=None, auth=True, headers=None):
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = "Bearer " + self.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), json.loads(payload) if payload else {}
        finally:
            connection.close()

    def upload(self, audio=None, **fields):
        body, content_type = multipart(audio, **fields)
        return self.request("POST", "/v1/audio/transcriptions", body, headers={"Content-Type": content_type})

    def test_health_models_auth_and_single_resident_instance(self):
        self.assertEqual(self.server.server.sockets[0].getsockname()[0], "127.0.0.1")
        status, headers, body = self.request(auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["model_loaded"])
        self.assertEqual(body["version"], "test")
        self.assertIn("X-Request-Id", headers)
        for path in ("/v1/models", "/v1/audio/transcriptions", "/v1/unknown"):
            self.assertEqual(self.request(path=path, auth=False)[0], 401)
        self.assertEqual(self.request(path="/v1/models", auth=False,
                                      headers={"Authorization": "Bearer incorrect"})[0], 401)
        self.assertEqual(self.request(path="/v1/models")[2]["data"][0]["id"], MODEL_ID)
        with patch("ml.loader.load_parakeet_model", side_effect=AssertionError("HTTP must never load a model")):
            status, headers, body = self.upload(wav_bytes(10), model="scribekit-parakeet", language="en", response_format="verbose_json")
        self.assertEqual((status, body), (200, {"text": "test transcript"}))
        self.assertIn("X-ScribeKit-Queue", headers)
        self.assertEqual(self.seen, [(self.model, 160000, "ScribeKitt-inference")])
        self.model = None
        self.assertFalse(self.request(auth=False)[2]["model_loaded"])
        self.assertEqual(self.request(path="/v1/models")[2]["data"], [])
        self.assertEqual(self.upload()[0], 503)

    def test_validation_and_limits(self):
        status, _, body = self.upload(model="other")
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": {"message": "unknown model", "type": "invalid_request_error", "code": "model_not_found"}})
        self.assertEqual(self.upload(response_format="text")[0], 400)
        status, _, body = self.upload(wav_bytes(0.01))
        self.assertEqual(status, 400)
        self.assertIn("0.1 seconds", body["error"]["message"])
        self.assertEqual(self.upload(b"not audio")[0], 400)
        self.assertEqual(self.request("POST", "/v1/audio/transcriptions", b"", headers={"Content-Length": str(MAX_BODY + 1)})[0], 413)

    def test_cors_is_loopback_allowlist_and_configurable(self):
        origin = "http://localhost:3000"
        status, headers, _ = self.request("OPTIONS", "/v1/audio/transcriptions", auth=False,
                                           headers={"Origin": origin})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], origin)
        for origin in ("https://evil.test", "http://localhost.evil.test:80", "null", "http://localhost:*"):
            self.assertEqual(self.request(headers={"Origin": origin})[0], 403)
        self.server.configure(True, 0, ["http://127.0.0.1:3000"])
        self.assertEqual(self.request(headers={"Origin": "http://localhost:3000"})[0], 403)
        self.assertEqual(self.request(headers={"Origin": "http://127.0.0.1:3000"})[0], 200)

    def test_token_permissions_ephemeral_port_disable_and_conflict(self):
        root = Path(self.tmp.name)
        self.assertEqual((root / "stt_server.token").stat().st_mode & 0o777, 0o600)
        self.assertEqual(int((root / "stt_server.port").read_text()), self.port)
        self.assertEqual(token_file(root / "stt_server.token"), self.token)
        self.server.configure(False)
        self.assertFalse((root / "stt_server.port").exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            result = self.server.configure(True, occupied.getsockname()[1])
            self.assertFalse(result["running"])
            self.assertIn("could not start", result["error"])

    def test_100_parallel_uploads_backpressure_then_dictation_first(self):
        self.scheduler.set_recording(True)
        with ThreadPoolExecutor(max_workers=100) as clients:
            pending = [clients.submit(self.upload) for _ in range(100)]
            deadline = time.monotonic() + 8
            while sum(f.done() for f in pending) < 96 and time.monotonic() < deadline:
                time.sleep(0.01)
            rejected = [f.result() for f in pending if f.done()]
            self.assertEqual(len(rejected), 96)
            self.assertTrue(all(r[0] == 429 and r[1]["Retry-After"] == "1" for r in rejected))
            self.assertEqual(self.scheduler.submit(lambda: "dictation first").result(1), "dictation first")
            self.assertEqual(len(self.seen), 0)
            self.scheduler.set_recording(False)
            results = [f.result(5) for f in pending]
        self.assertEqual(sum(r[0] == 200 for r in results), 4)
        self.assertEqual(self.request()[0], 200)

    def test_busy_disable_drains_503_without_waiting_for_chunk(self):
        started, release = threading.Event(), threading.Event()
        def slow(model, samples):
            started.set()
            release.wait(5)
            return "done"
        self.server.infer = slow
        with ThreadPoolExecutor(max_workers=1) as client:
            pending = client.submit(self.upload)
            self.assertTrue(started.wait(2))
            begin = time.monotonic()
            self.server.configure(False)
            self.assertLess(time.monotonic() - begin, 1)
            self.assertEqual(pending.result(1)[0], 503)
            release.set()

    def test_chunked_transfer_and_disconnect_cancel_waiting_job(self):
        body, content_type = multipart()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("POST", "/v1/audio/transcriptions", iter([body[:17], body[17:]]),
                           {"Authorization": "Bearer " + self.token, "Content-Type": content_type}, encode_chunked=True)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        connection.close()
        self.scheduler.set_recording(True)
        client = socket.create_connection(("127.0.0.1", self.port))
        headers = (f"POST /v1/audio/transcriptions HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
                   f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n").encode()
        client.sendall(headers + body)
        deadline = time.monotonic() + 2
        while not self.scheduler.api and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.scheduler.api), 1)
        client.close()
        while self.scheduler.api and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.scheduler.api), 0)


class AudioTests(unittest.TestCase):
    def test_chunk_windows_and_overlap_join(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio"
            np.ones(60 * 16000, dtype="<f4").tofile(path)
            chunks = list(audio_chunks(path))
            self.assertEqual([len(c[0]) for c in chunks], [400000, 400000, 176000])
            self.assertEqual([c[1] for c in chunks], [False, True, True])
            self.assertEqual(join_text("Hello, world", "world again.", True), "Hello, world again.")
            self.assertEqual(join_text("yes", "yes", False), "yes yes")
            np.zeros(60 * 16000, dtype="<f4").tofile(path)
            quiet = list(audio_chunks(path))
            self.assertTrue(all(not overlap for _, overlap in quiet))
            self.assertEqual(sum(len(samples) for samples, _ in quiet), 60 * 16000)

    def test_real_decoding_all_requested_formats(self):
        import av
        formats = [("wav", "pcm_s16le"), ("mp3", "libmp3lame"), ("ipod", "aac"), ("adts", "aac"),
                   ("flac", "flac"), ("ogg", "libopus"), ("webm", "libopus")]
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            for container_format, codec in formats:
                with self.subTest(format=container_format):
                    with av.open(str(source), "w", format=container_format) as container:
                        stream = container.add_stream(codec, rate=48000)
                        stream.layout = "stereo"
                        samples = np.zeros((2, 48000), dtype=np.float32)
                        frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
                        frame.sample_rate = 48000
                        for packet in stream.encode(frame):
                            container.mux(packet)
                        for packet in stream.encode(None):
                            container.mux(packet)
                    seconds = decode_audio(source, target, threading.Event())
                    self.assertAlmostEqual(seconds, 1, delta=0.1)
                    self.assertEqual(target.stat().st_size, int(seconds * 16000) * 4)

    def test_duration_and_playlist_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            source.write_bytes(wav_bytes(1))
            with patch("ml.stt_audio.MAX_SECONDS", 0.5):
                with self.assertRaisesRegex(AudioError, "2 hours"):
                    decode_audio(source, target, threading.Event())
            source.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:9/private\n")
            with self.assertRaises(AudioError):
                decode_audio(source, target, threading.Event())

    def test_multipart_byte_boundaries_do_not_corrupt_audio(self):
        audio = b"abc\r\n--ScribeKitt-test-boundaryNOT-a-boundary\x00\xff"
        body, content_type = multipart(audio, language="en")
        with tempfile.TemporaryDirectory() as directory:
            upload = MultipartUpload(content_type, Path(directory))
            try:
                for byte in body:
                    upload.feed(bytes([byte]))
                upload.finish()
                self.assertEqual(upload.file.read_bytes(), audio)
                self.assertEqual(upload.fields, {"language": "en"})
            finally:
                upload.close()


class DaemonIntegrationTests(unittest.TestCase):
    def test_rpc_remains_responsive_and_sigterm_drains_busy_http(self):
        # Exercise the actual daemon main loop, with a sentinel in its real
        # MODEL_CACHE. No MLX import or weights are needed for this test.
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
import functools, os, sys, time
from pathlib import Path
from ml import rpc, stt_server
from ml.loader import MODEL_CACHE
from ml.parakeet import DEFAULT_PARAKEET_REPO
root = Path(sys.argv[1])
MODEL_CACHE[("parakeet", DEFAULT_PARAKEET_REPO)] = object()
def infer(model, samples):
    print("library progress on stderr", flush=True)
    (root / "started").touch()
    time.sleep(30)
    return "unused"
stt_server.STTServer = functools.partial(stt_server.STTServer, support=root, infer=infer)
os._exit(rpc.main())
'''
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "Sources"),
                       SCRIBE_STT_ENABLED="1", SCRIBE_STT_PORT="0", SCRIBE_STT_VERSION="integration-test")
            process = subprocess.Popen([sys.executable, "-u", "-c", script, directory], env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                process.stdin.write('{"id":1,"method":"stt_status"}\n')
                process.stdin.flush()
                status = json.loads(process.stdout.readline())["result"]
                self.assertTrue(status["running"])
                token = (Path(directory) / "stt_server.token").read_text().strip()
                body, content_type = multipart()
                connection = http.client.HTTPConnection("127.0.0.1", status["port"], timeout=3)
                connection.request("POST", "/v1/audio/transcriptions", body,
                                   {"Authorization": "Bearer " + token, "Content-Type": content_type})
                deadline = time.monotonic() + 3
                while not (Path(directory) / "started").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((Path(directory) / "started").exists())
                process.stdin.write('{"id":2,"method":"ping"}\n')
                process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())["result"], {"pong": True})
                begin = time.monotonic()
                process.terminate()
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                connection.close()
                process.wait(timeout=1)
                self.assertLess(time.monotonic() - begin, 1)
                self.assertIn("library progress on stderr", process.stderr.read())
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", status["port"]), timeout=0.2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for handle in (process.stdin, process.stdout, process.stderr):
                    handle.close()


if __name__ == "__main__":
    unittest.main()
