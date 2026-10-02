"""JSON-RPC stdin/stdout server for ML tasks."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from contextlib import nullcontext, redirect_stdout
from typing import Any, Dict

from .loader import load_parakeet_model
from .parakeet import DEFAULT_PARAKEET_REPO, transcribe
from .preview import sessions

_protocol_output = None
_output_lock = threading.Lock()
_scheduler = None
_server = None


def _respond(payload: Dict[str, Any]) -> None:
    with _output_lock:
        output = _protocol_output or sys.stdout
        output.write(json.dumps(payload) + "\n")
        output.flush()


def _execute(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if method == "stt_configure":
        return _server.configure(params.get("enabled", True), params.get("port", 8111), params.get("origins"))
    if method == "stt_status":
        return _server.status()
    if method == "recording_state":
        _scheduler.set_recording(params.get("active", False))
        return {"success": True}
    if method == "ping":
        return {"pong": True}
    if method == "prepare_model":
        from .setup import prepare_model
        return prepare_model()
    if method == "verify_setup":
        from .setup import verify_model
        return verify_model()
    if method == "transcribe":
        repo = params.get("repo") or DEFAULT_PARAKEET_REPO
        pcm_path = params.get("pcm_path")
        if not pcm_path:
            raise ValueError("pcm_path is required for transcribe")
        # The final pass always owns the model; release only provisional state.
        sessions.clear()
        return transcribe(repo, pcm_path)
    if method == "preview_start":
        return sessions.start(params.get("session_id"), params.get("repo") or DEFAULT_PARAKEET_REPO)
    if method == "preview_audio":
        return sessions.append(params.get("session_id"), params.get("sequence"), params.get("audio_b64"))
    if method == "preview_end":
        session_id = params.get("session_id")
        if not session_id:
            raise ValueError("session_id is required")
        return sessions.clear(session_id)
    if method == "warmup":
        warm_type = params.get("type")
        repo = params.get("repo")
        if not warm_type or not repo:
            raise ValueError("warmup requires 'type' and 'repo'")
        if warm_type == "parakeet":
            load_parakeet_model(repo)
        else:
            raise ValueError(f"Unknown warmup type: {warm_type}")
        return {"success": True}
    raise ValueError(f"Unknown method: {method}")


def _handle_request(request: Any) -> None:
    req_id = request.get("id") if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict):
            raise ValueError("Request must be an object")
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        # Model libraries may print progress; stdout is reserved for RPC frames.
        with nullcontext() if _protocol_output is not None else redirect_stdout(sys.stderr):
            result = _execute(request.get("method"), params)
        _respond({"jsonrpc": "2.0", "id": req_id, "result": result})
    except Exception as exc:
        _respond({"jsonrpc": "2.0", "id": req_id, "error": {"message": str(exc)}})


def main(*, drain_rpc_on_eof=False) -> int:
    global _protocol_output, _scheduler, _server
    from .inference import InferenceScheduler
    from .stt_server import STTServer
    _protocol_output = sys.stdout
    _scheduler = InferenceScheduler()
    _scheduler.set_recording(os.environ.get("SCRIBE_RECORDING_ACTIVE") == "1")
    _server = STTServer(_scheduler, version=os.environ.get("SCRIBE_STT_VERSION", "unknown"))
    previous_signal = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        # Reply 503 and close the listener before exiting, without joining GPU
        # inference or CPU decoding. Normal EOF below also handles parent exit.
        try:
            _server.close()
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, terminate)
    futures = []
    try:
        # Redirect once for the daemon lifetime. Per-thread redirect_stdout is
        # process-global and would race HTTP/model output against RPC replies.
        with redirect_stdout(sys.stderr):
            if os.environ.get("SCRIBE_STT_ENABLED") == "1":
                try:
                    _server.configure(True, int(os.environ.get("SCRIBE_STT_PORT", "8111")),
                                      json.loads(os.environ.get("SCRIBE_STT_ORIGINS", "null")))
                except Exception as exc:
                    # Bad API preferences must not take down local dictation.
                    _server.problem = f"Localhost API could not start: {exc}"
                if _server.problem:
                    print(_server.problem, file=sys.stderr)
            for line in sys.stdin:
                if not line.strip():
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    _respond({"jsonrpc": "2.0", "id": None, "error": {"message": f"Invalid JSON: {exc}"}})
                    continue
                method = request.get("method") if isinstance(request, dict) else None
                if method in ("stt_configure", "stt_status", "recording_state", "ping"):
                    _handle_request(request)
                else:
                    # Read the next frame immediately: dictation priority must
                    # be visible even while the worker finishes an HTTP chunk.
                    futures = [f for f in futures if not f.done()]
                    futures.append(_scheduler.submit(lambda request=request: _handle_request(request)))
            # Optional graceful RPC drain for protocol tests; app EOF is quit.
            if drain_rpc_on_eof:
                for future in futures:
                    future.result()
    finally:
        _server.close()
        _scheduler.close()
        signal.signal(signal.SIGTERM, previous_signal)
        _protocol_output = None
    return 0
