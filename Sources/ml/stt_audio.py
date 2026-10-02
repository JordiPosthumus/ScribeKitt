"""Bounded CPU decoding and short chunks. No model loading occurs here."""
from pathlib import Path
import re
import threading

SAMPLE_RATE = 16000
MAX_SECONDS = 7200
MIN_SECONDS = 0.1


class AudioError(ValueError):
    pass


def decode_audio(source: Path, target: Path, cancelled: threading.Event):
    try:
        import av
    except ImportError as exc:
        raise AudioError("Audio decoder unavailable; update the ScribeKitt runtime (PyAV required)") from exc
    count = 0
    try:
        with av.open(str(source), options={
            "format_whitelist": "wav,mp3,mov,aac,flac,ogg,matroska,webm",
            "protocol_whitelist": "file", "enable_drefs": "0", "use_absolute_path": "0",
            "probesize": "5000000", "analyzeduration": "5000000", "max_streams": "8",
        }) as container, target.open("wb") as output:
            if not container.streams.audio:
                raise AudioError("Upload contains no audio stream")
            stream = container.streams.audio[0]
            duration = (float(stream.duration * stream.time_base) if stream.duration is not None
                        else container.duration / av.time_base if container.duration is not None else None)
            if duration is not None and duration > MAX_SECONDS:
                raise AudioError("Audio exceeds the maximum duration of 2 hours")
            resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)

            def write(frames):
                nonlocal count
                for frame in frames:
                    if cancelled.is_set():
                        raise AudioError("Request cancelled")
                    count += frame.samples
                    if count > MAX_SECONDS * SAMPLE_RATE:
                        raise AudioError("Audio exceeds the maximum duration of 2 hours")
                    output.write(frame.to_ndarray().astype("<f4", copy=False).tobytes())

            for frame in container.decode(stream):
                if cancelled.is_set():
                    raise AudioError("Request cancelled")
                write(resampler.resample(frame))
            write(resampler.resample(None))
        if count < MIN_SECONDS * SAMPLE_RATE:
            raise AudioError("Audio must contain at least 0.1 seconds of decoded sound")
        return count / SAMPLE_RATE
    except AudioError:
        raise
    except Exception as exc:
        raise AudioError("Cannot decode audio; use wav, mp3, m4a/aac, flac, ogg/opus, or webm") from exc


def audio_chunks(path):
    """20–25 s windows, cut at quiet 40 ms frames; otherwise overlap 0.5 s."""
    import numpy as np
    total = path.stat().st_size // 4
    start = 0
    overlap = False
    with path.open("rb") as stream:
        while start < total:
            stream.seek(start * 4)
            samples = np.frombuffer(stream.read(25 * SAMPLE_RATE * 4), dtype="<f4")
            end = start + len(samples)
            next_overlap = False
            if end < total:
                tail = samples[20 * SAMPLE_RATE:]
                width = 640
                energy = np.mean(tail[:len(tail) // width * width].reshape(-1, width) ** 2, axis=1)
                quiet = int(np.argmin(energy))
                if energy[quiet] < 0.0001:
                    cut = 20 * SAMPLE_RATE + quiet * width + width // 2
                    samples, end = samples[:cut], start + cut
                else:
                    next_overlap = True
            yield samples, overlap
            start = end - (SAMPLE_RATE // 2 if next_overlap else 0)
            overlap = next_overlap


def join_text(previous, incoming, overlap):
    incoming = incoming.strip()
    if overlap and previous:
        old, new = previous.split(), incoming.split()
        normalize = lambda s: re.sub(r"[^\w]", "", s).casefold()
        for size in range(min(12, len(old), len(new)), 0, -1):
            if [normalize(w) for w in old[-size:]] == [normalize(w) for w in new[:size]]:
                incoming = " ".join(new[size:])
                break
    return " ".join(part for part in (previous, incoming) if part)
