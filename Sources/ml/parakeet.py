"""Parakeet transcription helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from .loader import load_parakeet_model
from .stt_audio import MAX_SECONDS, MIN_SECONDS, SAMPLE_RATE, audio_chunks, join_text

DEFAULT_PARAKEET_REPO = "mlx-community/parakeet-tdt-0.6b-v2"

# Full-attention decoding stays single-pass for short recordings. Longer
# dictations reuse the HTTP chunking so peak memory stays bounded; an
# unbounded single pass over hours of audio produced the jetsam kills.
SINGLE_PASS_MAX_SECONDS = 120


def extract_parakeet_text(result: Any) -> str:
    if isinstance(result, list) and result:
        first_item = result[0]
        if hasattr(first_item, "text"):
            return first_item.text or ""
        return str(first_item)

    if hasattr(result, "text"):
        return result.text or ""
    if hasattr(result, "texts") and getattr(result, "texts"):
        return result.texts[0] or ""

    if isinstance(result, dict):
        if "text" in result:
            return result.get("text", "") or ""
        if "texts" in result and result.get("texts"):
            return result["texts"][0] or ""

    raise AttributeError(f"Cannot extract text from result: {result}")


def release_gpu_cache() -> None:
    """Drop the Metal allocator cache so finished work returns GPU memory.

    MLX retains freed buffers in its cache until the cache limit is reached;
    without an explicit release, long sessions keep resident memory near the
    peak of the largest job. Hosts without a Metal device skip this harmlessly.
    """
    try:
        import mlx.core as mx

        # Prefer the current top-level API; older MLX only exposes the alias.
        clear_cache = getattr(mx, "clear_cache", None) or mx.metal.clear_cache
        clear_cache()
    except Exception:
        pass


def transcribe(repo: str, pcm_path: str) -> Dict[str, Any]:
    return transcribe_path(repo, pcm_path)


def transcribe_path(repo: str, pcm_path: str, loader=None, transcribe_fn=None) -> Dict[str, Any]:
    """Transcribe a dictation PCM file with bounded duration and memory.

    Recordings beyond SINGLE_PASS_MAX_SECONDS reuse the 20-25 second chunking,
    so an hours-long dictation can never request the unbounded full-attention
    pass that spiked resident memory under pressure. The duration bound uses
    the file size alone, so oversize input is rejected before any model loads.
    """
    if not os.path.exists(pcm_path):
        raise FileNotFoundError(f"PCM file not found: {pcm_path}")
    if not os.access(pcm_path, os.R_OK):
        raise PermissionError(f"Cannot read PCM file: {pcm_path}")

    total_samples = os.path.getsize(pcm_path) // 4
    # Integer sample counts match the HTTP path exactly: a 7200 s recording is
    # accepted, one sample more is not, and sub-minimum audio fails loudly
    # instead of reaching the model with an empty array.
    if total_samples > MAX_SECONDS * SAMPLE_RATE:
        raise ValueError(f"Audio exceeds the maximum duration of {MAX_SECONDS // 3600} hours")
    if total_samples < MIN_SECONDS * SAMPLE_RATE:
        raise ValueError(f"Audio must contain at least {MIN_SECONDS} seconds of decoded sound")

    model = (loader or load_parakeet_model)(repo)
    decode = transcribe_fn or transcribe_samples
    if total_samples <= SINGLE_PASS_MAX_SECONDS * SAMPLE_RATE:
        import numpy as np

        audio_data = np.fromfile(pcm_path, dtype=np.float32)
        return {"success": True, "text": decode(model, audio_data)}
    text = ""
    for samples, overlap in audio_chunks(Path(pcm_path)):
        text = join_text(text, decode(model, samples), overlap)
    return {"success": True, "text": text}


def transcribe_samples(model, audio_data) -> str:
    """Decode with the caller's model. In particular, HTTP never calls a loader."""
    try:
        import mlx.core as mx
    except ImportError as exc:
        raise RuntimeError(f"mlx.core import failed: {exc}") from exc

    try:
        from parakeet_mlx.audio import get_logmel
    except ImportError as exc:
        raise RuntimeError(f"parakeet_mlx.audio import failed: {exc}") from exc

    # Every caller supplies float32 (rpc fromfile, HTTP chunk buffers); an
    # extra astype copy here doubled the largest recording's memory for nothing.
    audio_mlx = mx.array(audio_data)
    mel = get_logmel(audio_mlx, model.preprocessor_config)
    result = model.generate(mel)
    release_gpu_cache()

    return extract_parakeet_text(result)
