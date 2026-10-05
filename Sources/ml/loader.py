"""Model loading and caching utilities for Parakeet MLX."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Tuple

# Keep HF from grabbing a token implicitly; don't force offline globally here.
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MODEL_CACHE: Dict[Tuple[str, str], Any] = {}
# Distinct repos rarely exceed one; the cap only prevents unbounded growth if
# future builds ever switch repos inside one daemon lifetime.
MAX_CACHED_MODELS = 2
HF_ENV_KEYS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


def _set_offline_env() -> Dict[str, str]:
    """Enable offline flags, returning previous values for restoration."""
    previous = {k: os.environ.get(k) for k in HF_ENV_KEYS}
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    return previous


def _restore_env(previous: Dict[str, str]) -> None:
    """Restore HF offline flags to their prior state."""
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def load_parakeet_model(repo: str):
    cache_key = ("parakeet", repo)
    if cache_key in MODEL_CACHE:
        return MODEL_CACHE[cache_key]

    try:
        from parakeet_mlx import from_pretrained
    except Exception as exc:
        raise RuntimeError(f"parakeet-mlx import failed: {exc}") from exc

    previous = _set_offline_env()
    try:
        from huggingface_hub import hf_hub_download

        # Hub libraries may cache their offline flag at import time. Resolve the
        # existing files explicitly offline, then pass the snapshot directory to
        # Parakeet so loading cannot make a metadata request. Decoder defaults,
        # weights and the process-level model cache remain unchanged.
        snapshot = Path(repo)
        if not snapshot.is_dir():
            config = Path(hf_hub_download(repo, "config.json", local_files_only=True))
            weights = Path(hf_hub_download(repo, "model.safetensors", local_files_only=True))
            if config.parent != weights.parent:
                raise RuntimeError("Cached model files refer to different snapshots")
            snapshot = config.parent
        model = from_pretrained(str(snapshot))
    except Exception as exc:
        _restore_env(previous)
        raise RuntimeError(f"Model not available offline: {exc}") from exc
    _restore_env(previous)

    MODEL_CACHE[cache_key] = model
    while len(MODEL_CACHE) > MAX_CACHED_MODELS:
        MODEL_CACHE.pop(next(iter(MODEL_CACHE)))
    return model


def configure_memory_limits() -> None:
    """Bound the Metal allocator cache so finished jobs release GPU memory.

    MLX keeps freed buffers up to a default cache limit that scales with total
    RAM, so an idle daemon could otherwise stay resident near its historical
    peak. A quarter of physical memory (max 6 GiB) still covers the resident
    Parakeet model with working headroom while letting RSS fall after jobs.
    """
    try:
        import mlx.core as mx

        try:
            total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            total_bytes = 0
        cache_limit = min(6 * 1024**3, total_bytes // 4) if total_bytes else 4 * 1024**3
        # Prefer the current top-level API; older MLX only exposes the alias.
        set_cache_limit = getattr(mx, "set_cache_limit", None) or mx.metal.set_cache_limit
        set_cache_limit(cache_limit)
    except Exception:
        # Non-Metal hosts and older MLX keep the default allocator behavior.
        pass
