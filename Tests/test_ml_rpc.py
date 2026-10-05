"""Protocol regressions without loading or downloading any models."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
from ml import rpc
from ml.parakeet import transcribe_path, transcribe_samples
from ml.preview import PreviewSessions, merge_preview_tokens
from ml.stt_audio import MAX_SECONDS, SAMPLE_RATE
from types import SimpleNamespace


class RPCRegressionTests(unittest.TestCase):
    def test_bad_api_preferences_do_not_break_dictation_daemon(self):
        out, err = io.StringIO(), io.StringIO()
        requests = '\n'.join(json.dumps(r) for r in [
            {"id": 1, "method": "ping"}, {"id": 2, "method": "stt_status"}])
        with patch.dict("os.environ", {"SCRIBE_STT_ENABLED": "1", "SCRIBE_STT_PORT": "-1"}), \
             patch.object(sys, "stdin", io.StringIO(requests)), patch.object(sys, "stdout", out), \
             patch.object(sys, "stderr", err):
            self.assertEqual(rpc.main(drain_rpc_on_eof=True), 0)
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"], {"pong": True})
        self.assertFalse(responses[1]["result"]["running"])
        self.assertIn("Port", responses[1]["result"]["error"])

    def test_invalid_requests_do_not_kill_daemon(self):
        invalid = [None, [], 42, "text", {"id": 5, "method": "ping", "params": []}]
        lines = [json.dumps(value) for value in invalid]
        lines += ['{bad', json.dumps({"id": 7, "method": "ping"})]
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("\n".join(lines))), patch.object(sys, "stdout", out):
            self.assertEqual(rpc.main(drain_rpc_on_eof=True), 0)
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(responses), 7)
        self.assertTrue(all("error" in response for response in responses if response["id"] != 7))
        self.assertEqual(next(r for r in responses if r["id"] == 7)["result"], {"pong": True})

    def test_model_logging_does_not_corrupt_responses(self):
        def noisy_transcribe(repo, path):
            print("Loading model...")
            return {"success": True, "text": "hello"}

        out, err = io.StringIO(), io.StringIO()
        with patch.object(rpc, "transcribe_path", noisy_transcribe), patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            rpc._handle_request({"id": 1, "method": "transcribe", "params": {"pcm_path": "test.raw"}})
        self.assertEqual(json.loads(out.getvalue())["result"]["text"], "hello")
        self.assertIn("Loading model", err.getvalue())

    def test_stdout_is_restored_after_model_error(self):
        out = io.StringIO()
        with patch.object(rpc, "load_parakeet_model", side_effect=RuntimeError("load failed")), patch.object(sys, "stdout", out):
            rpc._handle_request({"id": 3, "method": "warmup", "params": {"type": "parakeet", "repo": "test"}})
            rpc._handle_request({"id": 4, "method": "ping"})
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["message"], "load failed")
        self.assertEqual(responses[1]["result"], {"pong": True})

    def test_final_pass_releases_preview_before_transcribing(self):
        def transcribe_path(repo, path):
            self.assertIsNone(rpc.sessions.model)
            return {"success": True, "text": "final"}
        rpc.sessions.session_id = "old"
        rpc.sessions.model = object()
        with patch.object(rpc, "transcribe_path", transcribe_path):
            self.assertEqual(rpc._execute("transcribe", {"pcm_path": "sample"})["text"], "final")


class FakeEncoder:
    def __init__(self):
        self.layers = [SimpleNamespace(self_attn="original")]
        self.kind = "original"

    def set_attention_model(self, name, context):
        self.kind = name
        for layer in self.layers:
            layer.self_attn = "preview"


class FakeModel:
    def __init__(self):
        self.encoder = FakeEncoder()

    def transcribe_stream(self, **kwargs):
        return object()


class PreviewRegressionTests(unittest.TestCase):
    def test_preview_never_changes_final_model_attention(self):
        model = FakeModel()
        sessions = PreviewSessions(loader=lambda repo: model)
        sessions.start("one", "cached-model")
        self.assertEqual(model.encoder.kind, "original")
        self.assertEqual(model.encoder.layers[0].self_attn, "original")
        sessions.clear("one")
        self.assertIsNone(sessions.model)

    def test_late_cleanup_cannot_end_a_new_recording(self):
        model = FakeModel()
        sessions = PreviewSessions(loader=lambda _: model)
        sessions.start("old", "model")
        sessions.start("new", "model")
        active = sessions.model
        sessions.clear("old")
        self.assertIs(sessions.model, active)
        self.assertFalse(sessions.append("old", 0, "unused")["active"])
        self.assertIs(sessions.model, active)

    def test_invalid_audio_is_rejected_before_model_inference(self):
        model = FakeModel()
        sessions = PreviewSessions(loader=lambda _: model)
        sessions.start("one", "model")
        for payload in ["", "a!", "YWJj"]:
            with self.assertRaises(ValueError):
                sessions.append("one", 0, payload)
        with self.assertRaisesRegex(ValueError, "out of order"):
            sessions.append("one", 2, "unused")


class PreviewTextContinuityTests(unittest.TestCase):
    @staticmethod
    def token(identifier, start):
        return SimpleNamespace(id=identifier, start=start, end=start + 0.3)

    def test_overlap_retains_earlier_words_and_uses_new_draft(self):
        old = [self.token(i, i) for i in range(6)]
        new = [self.token(i, i + 0.05) for i in range(3, 8)]
        merged = merge_preview_tokens(old, new, 2.8)
        self.assertEqual([t.id for t in merged], list(range(8)))
        self.assertIs(merged[3], new[0])

    def test_timestamps_disambiguate_repeated_phrases(self):
        old = [self.token(i % 2, i) for i in range(10)]
        new = [self.token(0, 8.05), self.token(1, 9.05), self.token(2, 10)]
        merged = merge_preview_tokens(old, new, 7.8)
        self.assertEqual([t.id for t in merged], [i % 2 for i in range(10)] + [2])

    def test_initial_window_can_rewrite_early_draft(self):
        old = [self.token(1, 0)]
        corrected = [self.token(2, 0), self.token(3, 1)]
        self.assertEqual(merge_preview_tokens(old, corrected, 0), corrected)

    def test_silence_keeps_already_spoken_words(self):
        old = [self.token(1, 0)]
        self.assertEqual(merge_preview_tokens(old, [], 0), old)
        self.assertEqual(merge_preview_tokens(old, [], 8), old)
        later = [self.token(2, 12)]
        self.assertEqual(merge_preview_tokens(old, later, 8), old + later)


class TranscribePathTests(unittest.TestCase):
    @staticmethod
    def _fake_decode(calls, text):
        def decode(model, samples):
            calls.append(len(samples))
            return text
        return decode

    def test_oversize_dictation_is_rejected_before_any_model_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.f32"
            with open(path, "wb") as handle:
                handle.truncate((MAX_SECONDS + 1) * SAMPLE_RATE * 4)
            with patch("ml.parakeet.load_parakeet_model",
                       side_effect=AssertionError("oversize input must never load a model")):
                with self.assertRaises(ValueError):
                    transcribe_path("repo", str(path))

    def test_boundary_and_minimum_durations_follow_the_http_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Exactly at the bound: accepted (parity with the HTTP path's >).
            with open(root / "exact.f32", "wb") as handle:
                handle.truncate(2 * SAMPLE_RATE * 4)
            with patch("ml.parakeet.MAX_SECONDS", 2), \
                 patch("ml.parakeet.load_parakeet_model", return_value=object()), \
                 patch("ml.parakeet.transcribe_samples", return_value="ok"):
                self.assertEqual(transcribe_path("repo", str(root / "exact.f32"))["text"], "ok")
            # One sample past the bound: rejected before any model load.
            with open(root / "over.f32", "wb") as handle:
                handle.truncate((2 * SAMPLE_RATE + 1) * 4)
            with patch("ml.parakeet.MAX_SECONDS", 2), \
                 patch("ml.parakeet.load_parakeet_model",
                       side_effect=AssertionError("oversize input must never load a model")):
                with self.assertRaises(ValueError):
                    transcribe_path("repo", str(root / "over.f32"))
            # Below the minimum: rejected before any model load.
            (root / "short.f32").write_bytes(b"\0\0\0\0")
            with patch("ml.parakeet.load_parakeet_model",
                       side_effect=AssertionError("sub-minimum input must never load a model")):
                with self.assertRaises(ValueError):
                    transcribe_path("repo", str(root / "short.f32"))

    def test_short_dictation_stays_single_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.f32"
            np.zeros(SAMPLE_RATE, dtype="<f4").tofile(path)
            calls = []
            result = transcribe_path("repo", str(path), loader=lambda repo: object(),
                                     transcribe_fn=self._fake_decode(calls, "text"))
            self.assertEqual(result, {"success": True, "text": "text"})
            self.assertEqual(calls, [SAMPLE_RATE])

    def test_long_dictation_reuses_the_http_chunking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.f32"
            np.zeros(30 * SAMPLE_RATE, dtype="<f4").tofile(path)
            calls = []
            with patch("ml.parakeet.SINGLE_PASS_MAX_SECONDS", 0):
                result = transcribe_path("repo", str(path), loader=lambda repo: object(),
                                         transcribe_fn=self._fake_decode(calls, "hello"))
            self.assertEqual(result, {"success": True, "text": "hello hello"})
            self.assertEqual(len(calls), 2)
            self.assertEqual(sum(calls), 30 * SAMPLE_RATE)


class TranscribeSamplesTests(unittest.TestCase):
    def test_finished_decodes_release_the_metal_cache(self):
        cleared = []
        model = SimpleNamespace(preprocessor_config=object(), generate=lambda mel: None)
        # release_gpu_cache prefers the current top-level API and falls back to
        # the mlx.metal alias on older runtimes; exactly one binding fires.
        with patch("parakeet_mlx.audio.get_logmel", return_value=object()), \
             patch("mlx.core.clear_cache", side_effect=lambda: cleared.append(True)), \
             patch("mlx.core.metal.clear_cache", side_effect=lambda: cleared.append(True)):
            with patch("ml.parakeet.extract_parakeet_text", return_value="done"):
                self.assertEqual(transcribe_samples(model, np.zeros(1600, dtype=np.float32)), "done")
        self.assertEqual(len(cleared), 1)

    def test_missing_mlx_reports_a_friendly_error(self):
        model = SimpleNamespace(preprocessor_config=object())
        with patch.dict("sys.modules", {"mlx": None, "mlx.core": None}):
            with self.assertRaises(RuntimeError) as context:
                transcribe_samples(model, np.zeros(1600, dtype=np.float32))
        self.assertIn("mlx.core import failed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
