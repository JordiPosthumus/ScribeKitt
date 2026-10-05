"""Live drafts from a rolling audio window using the unchanged cached model.

Normal full-attention decoding avoids the severe accuracy loss of the installed
library's streaming approximation. Only eight seconds of preview PCM are kept;
the independent AAC recording still supplies the complete final transcription.
"""

from __future__ import annotations

import base64
from typing import Any

from .loader import load_parakeet_model
from .parakeet import extract_parakeet_text, release_gpu_cache


def merge_preview_tokens(previous, incoming, window_start):
    """Retain words leaving the audio window, replacing its overlapping draft.

    An aligned token run anchors the join even when the window starts inside a
    word. Times disambiguate repeated phrases. No additional inference is done.
    """
    if not incoming:
        return previous
    if not previous or window_start == 0:
        return incoming
    if previous[-1].end <= incoming[0].start:
        return previous + incoming
    best = (0, 0, 0)
    # Tokens that ended before the window cannot anchor the join: incoming
    # tokens start at window_start. Scanning only the recent tail keeps the
    # per-append cost bounded for hours-long sessions; time-ordered tokens
    # place all matchable candidates there, and the time-boundary fallback
    # below already covers the no-anchor case.
    first = len(previous)
    while first > 0 and previous[first - 1].end > window_start - 1.0:
        first -= 1
    for i in range(first, len(previous)):
        if previous[i].end < window_start:
            continue
        for j, new in enumerate(incoming):
            run = 0
            while i + run < len(previous) and j + run < len(incoming):
                a, b = previous[i + run], incoming[j + run]
                if a.id != b.id or abs(a.start - b.start) > 0.6:
                    break
                run += 1
            if run > best[0]:
                best = (run, i, j)
    if best[0] >= 2:
        return previous[:best[1]] + incoming[best[2]:]
    # With no reliable textual anchor, join once at a time boundary in the
    # overlap. The independently recorded final pass remains authoritative.
    cutoff = (previous[-1].end + incoming[0].start) / 2
    return [t for t in previous if t.end <= cutoff] + [t for t in incoming if t.end > cutoff]


class PreviewSessions:
    def __init__(self, loader=None):
        self.loader = loader or load_parakeet_model
        self.session_id = None
        self.model = None
        self.audio = bytearray()
        self.samples_seen = 0
        self.tokens = []
        self.sequence = 0

    def start(self, session_id: str, repo: str) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        self.clear()
        self.model = self.loader(repo)
        self.session_id = session_id
        return {"success": True}

    def append(self, session_id: str, sequence: int, audio_b64: str) -> dict[str, Any]:
        if session_id != self.session_id or self.model is None:
            return {"active": False, "stable": "", "draft": ""}
        if sequence != self.sequence:
            raise ValueError("Preview audio arrived out of order")
        raw = base64.b64decode(audio_b64, validate=True)
        if not raw or len(raw) % 4 or len(raw) > 16000 * 4 * 8:
            raise ValueError("Preview requires up to eight seconds of mono 16 kHz Float32 PCM")
        import numpy as np
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        samples = np.frombuffer(raw, dtype="<f4")
        if not np.isfinite(samples).all():
            raise ValueError("Preview audio contains non-finite samples")
        self.audio.extend(raw)
        self.samples_seen += len(samples)
        del self.audio[:max(0, len(self.audio) - 16000 * 4 * 8)]
        # Re-decode the recent window so a bad partial word cannot corrupt later
        # updates. No attention swaps or stream decoder state here; the
        # allocator cache is released at session end, not per update.
        window = np.frombuffer(bytes(self.audio), dtype="<f4")
        mel = get_logmel(mx.array(window), self.model.preprocessor_config)
        result = self.model.generate(mel)
        offset = max(0, self.samples_seen - len(window)) / 16000
        from dataclasses import replace
        incoming = [replace(token, start=token.start + offset) for token in result[0].tokens]
        self.tokens = merge_preview_tokens(self.tokens, incoming, offset)
        stable = "".join(token.text for token in self.tokens if token.end <= offset)
        draft = "".join(token.text for token in self.tokens if token.end > offset)
        if not self.tokens:
            draft = extract_parakeet_text(result)
        self.sequence += 1
        return {
            "active": True,
            "stable": stable,
            "draft": draft if stable else draft.lstrip(),
        }

    def clear(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None or session_id == self.session_id:
            self.model = None
            self.audio.clear()
            self.samples_seen = 0
            self.tokens = []
            self.session_id = None
            self.sequence = 0
            release_gpu_cache()
        return {"success": True}


sessions = PreviewSessions()
