"""Regression checks for strictly local loading with unchanged decoder defaults."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
from ml.loader import MODEL_CACHE, load_parakeet_model


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.old_cache = dict(MODEL_CACHE)
        MODEL_CACHE.clear()

    def tearDown(self):
        MODEL_CACHE.clear()
        MODEL_CACHE.update(self.old_cache)
        self.directory.cleanup()

    def test_cached_snapshot_uses_no_online_lookup_or_decoder_overrides(self):
        model = object()
        with patch("huggingface_hub.hf_hub_download", side_effect=lambda repo, name, **kw: str(self.root / name)) as resolve, \
             patch("parakeet_mlx.from_pretrained", return_value=model) as load:
            self.assertIs(load_parakeet_model("example/model"), model)
            self.assertIs(load_parakeet_model("example/model"), model)
            load.assert_called_once_with(str(self.root))
            self.assertEqual(resolve.call_count, 2)
            self.assertTrue(all(call.kwargs == {"local_files_only": True} for call in resolve.call_args_list))

    def test_local_directory_remains_supported(self):
        with patch("huggingface_hub.hf_hub_download") as resolve, \
             patch("parakeet_mlx.from_pretrained", return_value=object()) as load:
            load_parakeet_model(str(self.root))
            resolve.assert_not_called()
            load.assert_called_once_with(str(self.root))

    def test_failure_restores_environment_and_does_not_cache_a_model(self):
        before = {key: os.environ.get(key) for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
        with patch("huggingface_hub.hf_hub_download", side_effect=RuntimeError("missing")):
            with self.assertRaisesRegex(RuntimeError, "not available offline"):
                load_parakeet_model("example/model")
        self.assertEqual({key: os.environ.get(key) for key in before}, before)
        self.assertFalse(MODEL_CACHE)

    def test_cache_evicts_oldest_beyond_the_cap(self):
        with patch("huggingface_hub.hf_hub_download",
                   side_effect=lambda repo, name, **kw: str(self.root / name)), \
             patch("parakeet_mlx.from_pretrained", return_value=object()):
            for repo in ("example/a", "example/b", "example/c"):
                load_parakeet_model(repo)
        self.assertEqual(set(MODEL_CACHE), {("parakeet", "example/b"), ("parakeet", "example/c")})

    def test_memory_limit_configuration_is_exception_safe(self):
        from ml.loader import configure_memory_limits

        configure_memory_limits()  # real host: must not raise
        with patch("os.sysconf", side_effect=ValueError("unsupported")):
            configure_memory_limits()  # fallback path: still must not raise


if __name__ == "__main__":
    unittest.main()
