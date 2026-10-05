"""Loopback HTTP API in the existing ML daemon; never creates/loads a model.

HTTP and CPU decoding run separately from the single inference worker. Admission
is bounded before body reads, uploads spool to private temporary files, and all
HTTP requests close their connection after one response (no keep-alive backlog).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import hmac
import json
import os
from pathlib import Path
import re
import resource
import secrets
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

from .loader import MODEL_CACHE
from .parakeet import DEFAULT_PARAKEET_REPO, transcribe_samples
from .stt_audio import SAMPLE_RATE, AudioError, audio_chunks, decode_audio, join_text

MODEL_ID = "parakeet-tdt-0.6b-v2"
MAX_BODY = 100 * 1024 * 1024
MAX_JOBS = 4
SUPPORT = Path.home() / "Library/Application Support/ScribeKit"


class HTTPError(Exception):
    def __init__(self, status, message, code=None):
        self.status, self.message, self.code = status, message, code


def error_body(status, message, code=None):
    return {"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error",
                      "code": code or str(status)}}


def memory_telemetry() -> dict:
    """Resident memory and MLX allocator state in MiB; unavailable keys are omitted."""
    telemetry = {}
    try:
        # ru_maxrss is bytes on macOS and kibibytes on Linux.
        divisor = 1048576 if sys.platform == "darwin" else 1024
        telemetry["rss_peak_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / divisor, 1)
    except Exception:
        pass
    try:
        import mlx.core as mx

        # Prefer the current top-level API; older MLX only exposes the alias.
        get_active = getattr(mx, "get_active_memory", None) or mx.metal.get_active_memory
        get_cache = getattr(mx, "get_cache_memory", None) or mx.metal.get_cache_memory
        telemetry["mlx_active_mb"] = round(get_active() / 1048576, 1)
        telemetry["mlx_cache_mb"] = round(get_cache() / 1048576, 1)
    except Exception:
        pass
    return telemetry


def _log(message: str) -> None:
    print(f"[stt] {message}", file=sys.stderr, flush=True)


def resident_model():
    return MODEL_CACHE.get(("parakeet", DEFAULT_PARAKEET_REPO))


def token_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = os.open(path, flags)
    with os.fdopen(fd, "r+") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Token must be a regular file owned by the current user")
        os.fchmod(handle.fileno(), 0o600)
        token = handle.read(4097).strip()
        if not token:
            token = secrets.token_urlsafe(32)
            handle.write(token + "\n")
            handle.flush()
        if len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError("Invalid token file")
        return token


class MultipartUpload:
    """Streaming multipart parser with bounded part headers and text fields."""
    def __init__(self, content_type, directory):
        message = Message()
        message["content-type"] = content_type
        boundary = message.get_param("boundary")
        if message.get_content_type() != "multipart/form-data" or not boundary or len(boundary) > 200:
            raise HTTPError(400, "Expected multipart/form-data with a boundary")
        try:
            self.marker = b"--" + boundary.encode("ascii")
        except UnicodeEncodeError:
            raise HTTPError(400, "Invalid multipart boundary")
        self.directory, self.buffer, self.state = directory, bytearray(), "first"
        self.file, self.current, self.fields = None, None, {}
        self.name, self.value, self.parts = None, bytearray(), 0

    def feed(self, data):
        self.buffer.extend(data)
        while True:
            if self.state == "first":
                if len(self.buffer) < len(self.marker) + 2:
                    return
                if not self.buffer.startswith(self.marker + b"\r\n"):
                    raise HTTPError(400, "Invalid multipart body")
                del self.buffer[:len(self.marker) + 2]
                self.state = "headers"
            elif self.state == "headers":
                end = self.buffer.find(b"\r\n\r\n")
                if end < 0:
                    if len(self.buffer) > 8192:
                        raise HTTPError(400, "Multipart headers too large")
                    return
                if end > 8192:
                    raise HTTPError(400, "Multipart headers too large")
                headers = parse_headers(bytes(self.buffer[:end]))
                del self.buffer[:end + 4]
                disposition = Message()
                disposition["content-disposition"] = headers.get("content-disposition", "")
                self.name = disposition.get_param("name", header="content-disposition")
                self.parts += 1
                if not self.name or self.parts > 32 or self.name in self.fields:
                    raise HTTPError(400, "Invalid or duplicate multipart field")
                self.value = bytearray()
                if self.name == "file":
                    if self.file:
                        raise HTTPError(400, "Exactly one file is required")
                    self.file = self.directory / "upload"
                    self.current = self.file.open("wb")
                self.state = "data"
            elif self.state == "data":
                delimiter = b"\r\n" + self.marker
                end = self.buffer.find(delimiter)
                if end < 0:
                    size = max(0, len(self.buffer) - len(delimiter) - 2)
                    self._write(bytes(self.buffer[:size]))
                    del self.buffer[:size]
                    return
                if len(self.buffer) < end + len(delimiter) + 2:
                    return
                suffix = bytes(self.buffer[end + len(delimiter):end + len(delimiter) + 2])
                if suffix not in (b"--", b"\r\n"):
                    # A boundary-like byte sequence inside the audio is data.
                    self._write(bytes(self.buffer[:end + 2]))
                    del self.buffer[:end + 2]
                    continue
                self._write(bytes(self.buffer[:end]))
                del self.buffer[:end + len(delimiter) + 2]
                if self.current:
                    self.current.close()
                    self.current = None
                else:
                    try:
                        self.fields[self.name] = self.value.decode("utf-8")
                    except UnicodeDecodeError:
                        raise HTTPError(400, "Multipart text fields must be UTF-8")
                self.state = "done" if suffix == b"--" else "headers"
            else:
                self.buffer.clear()
                return

    def _write(self, data):
        if self.current:
            self.current.write(data)
        else:
            self.value.extend(data)
            if len(self.value) > 4096:
                raise HTTPError(400, "Multipart field too large")

    def finish(self):
        if self.state != "done" or not self.file:
            raise HTTPError(400, "A complete multipart file field is required")

    def close(self):
        if self.current:
            self.current.close()


def parse_headers(raw):
    headers = {}
    for line in raw.decode("iso-8859-1").split("\r\n"):
        key, separator, value = line.partition(":")
        key = key.lower()
        if not separator or not key or key.strip() != key or key in headers:
            raise HTTPError(400, "Invalid or duplicate header")
        headers[key] = value.strip()
    return headers


class STTServer:
    def __init__(self, scheduler, support=SUPPORT, version="unknown", model_getter=resident_model,
                 infer=transcribe_samples, decoder=decode_audio):
        self.scheduler, self.support, self.version = scheduler, Path(support), version
        self.model_getter, self.infer, self.decoder = model_getter, infer, decoder
        self.started = time.monotonic()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="ScribeKitt-HTTP", daemon=True)
        self.server, self.token, self.port = None, None, None
        self.problem, self.jobs, self.clients = None, 0, set()
        self.origins = ["http://127.0.0.1:*", "http://localhost:*"]
        self.decoder_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ScribeKitt-audio")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def configure(self, enabled=True, port=8111, origins=None):
        return asyncio.run_coroutine_threadsafe(self._configure(enabled, port, origins), self.loop).result(2)

    def status(self):
        return {"running": self.server is not None, "port": self.port,
                "token_path": str(self.support / "stt_server.token"), "error": self.problem}

    async def _configure(self, enabled, port, origins):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
        if origins is not None:
            if not isinstance(origins, list) or not all(self._valid_origin_rule(o) for o in origins):
                raise ValueError("Origins must be http://localhost or http://127.0.0.1 with a port or :*")
            self.origins = origins
        if enabled and self.server and self.requested_port == port:
            return self.status()
        await self._stop()
        self.problem = None
        if enabled:
            try:
                self.token = token_file(self.support / "stt_server.token")
                self.server = await asyncio.start_server(self._client, "127.0.0.1", port, limit=16384,
                                                         backlog=128, reuse_address=False)
                self.port = self.server.sockets[0].getsockname()[1]
                self.requested_port = port
                target = self.support / "stt_server.port"
                fd, temporary = tempfile.mkstemp(dir=self.support, prefix=".stt-port-")
                with os.fdopen(fd, "w") as handle:
                    handle.write(str(self.port) + "\n")
                os.replace(temporary, target)
            except Exception as exc:
                await self._stop()
                self.problem = f"Localhost API could not start: {exc}"
        return self.status()

    async def _stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            try:
                (self.support / "stt_server.port").unlink(missing_ok=True)
            except OSError:
                pass
        self.port = None
        clients = list(self.clients)
        for task in clients:
            task.cancel()
        if clients:
            await asyncio.wait(clients, timeout=0.5)

    def close(self):
        try:
            asyncio.run_coroutine_threadsafe(self._stop(), self.loop).result(0.8)
        finally:
            self.decoder_pool.shutdown(wait=False, cancel_futures=True)
            self.loop.call_soon_threadsafe(self.loop.stop)

    @staticmethod
    def _valid_origin_rule(origin):
        if not isinstance(origin, str):
            return False
        if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::(?:[0-9]+|\*))?", origin):
            return False
        if origin in ("http://127.0.0.1:*", "http://localhost:*"):
            return True
        try:
            url = urlsplit(origin)
            return (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")
                    and not url.username and not url.password and not url.path and not url.query
                    and not url.fragment and (url.port is None or 0 < url.port <= 65535))
        except ValueError:
            return False

    def _allow_origin(self, origin):
        if not origin or "*" in origin or not self._valid_origin_rule(origin):
            return False
        return origin in self.origins or f"http://{urlsplit(origin).hostname}:*" in self.origins

    async def _respond(self, writer, status, body, request_id, origin=None, queue_ms=0):
        payload = b"" if status == 204 else json.dumps(body, ensure_ascii=False).encode()
        reasons = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                   404: "Not Found", 408: "Request Timeout", 413: "Payload Too Large", 429: "Too Many Requests",
                   500: "Internal Server Error", 503: "Service Unavailable"}
        headers = [f"HTTP/1.1 {status} {reasons.get(status, 'Error')}", "Content-Type: application/json",
                   f"Content-Length: {len(payload)}", "Connection: close", "Cache-Control: no-store",
                   f"X-Request-Id: {request_id}", f"X-ScribeKit-Queue: {int(queue_ms)}"]
        if status in (429, 503):
            headers.append("Retry-After: 1")
        if status == 401:
            headers.append('WWW-Authenticate: Bearer realm="ScribeKit"')
        if origin:
            headers += [f"Access-Control-Allow-Origin: {origin}", "Vary: Origin",
                        "Access-Control-Allow-Methods: GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers: Authorization, Content-Type",
                        "Access-Control-Expose-Headers: X-Request-Id, X-ScribeKit-Queue, Retry-After"]
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + payload)
        await asyncio.wait_for(writer.drain(), 0.2)

    async def _body(self, reader, headers, upload):
        transfer, length = headers.get("transfer-encoding"), headers.get("content-length")
        if transfer and length:
            raise HTTPError(400, "Ambiguous body framing")
        count = 0
        if transfer:
            if transfer.lower() != "chunked":
                raise HTTPError(400, "Unsupported transfer encoding")
            while True:
                line = await reader.readuntil(b"\r\n")
                if len(line) > 128:
                    raise HTTPError(400, "Invalid chunk header")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise HTTPError(400, "Invalid chunk size")
                if size < 0:
                    raise HTTPError(400, "Invalid chunk size")
                if size == 0:
                    if await reader.readexactly(2) != b"\r\n":
                        raise HTTPError(400, "Chunk trailers are not supported")
                    break
                count += size
                if count > MAX_BODY:
                    raise HTTPError(413, "Upload exceeds 100 MB")
                while size:
                    data = await reader.readexactly(min(size, 65536))
                    size -= len(data)
                    upload.feed(data)
                if await reader.readexactly(2) != b"\r\n":
                    raise HTTPError(400, "Invalid chunk delimiter")
        else:
            if not length or not length.isascii() or not length.isdigit():
                raise HTTPError(400, "Content-Length or chunked transfer encoding is required")
            size = int(length)
            if size > MAX_BODY:
                raise HTTPError(413, "Upload exceeds 100 MB")
            while size:
                data = await reader.readexactly(min(size, 65536))
                size -= len(data)
                upload.feed(data)
        upload.finish()

    async def _transcribe(self, upload, directory, cancelled):
        fields = upload.fields
        if fields.get("model", MODEL_ID) not in (MODEL_ID, "scribekit-parakeet"):
            raise HTTPError(400, "unknown model", "model_not_found")
        if fields.get("response_format", "json") not in ("json", "verbose_json"):
            raise HTTPError(400, "response_format must be json or verbose_json")
        pcm = directory / "audio.f32"
        await self.loop.run_in_executor(self.decoder_pool, self.decoder, upload.file, pcm, cancelled)
        text, queue_ms, chunks = "", 0, 0
        for samples, overlap in audio_chunks(pcm):
            def infer(samples=samples):
                model = self.model_getter()
                if model is None:
                    raise HTTPError(503, "Speech model is not loaded; use dictation to load it")
                return self.infer(model, samples)
            result, waited = await asyncio.wrap_future(self.scheduler.submit(infer, api=True))
            queue_ms += waited
            chunks += 1
            text = join_text(text, result, overlap)
        _log(f"job done: {pcm.stat().st_size // (4 * SAMPLE_RATE)}s audio, {chunks} chunk(s), "
             f"queue {queue_ms:.0f} ms, {json.dumps(memory_telemetry())}")
        return {"text": text}, queue_ms

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        request_id, origin, admitted = str(uuid.uuid4()), None, False
        cancelled, processing, disconnected = threading.Event(), None, None
        directory, upload = None, None
        # Bounded idle/header connections too; no thread is created per client.
        if len(self.clients) >= 128:
            try:
                await self._respond(writer, 429, error_body(429, "Too many connections"), request_id)
            finally:
                writer.close()
            return
        self.clients.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(raw) > 16384:
                raise HTTPError(400, "Headers too large")
            first, _, rest = raw[:-4].partition(b"\r\n")
            try:
                method, path, protocol = first.decode("ascii").split(" ")
            except ValueError:
                raise HTTPError(400, "Invalid request line")
            if protocol not in ("HTTP/1.0", "HTTP/1.1"):
                raise HTTPError(400, "Unsupported HTTP version")
            headers = parse_headers(rest)
            if headers.get("origin"):
                if not self._allow_origin(headers["origin"]):
                    raise HTTPError(403, "Origin is not allowed")
                origin = headers["origin"]
            if method == "OPTIONS" and origin and path.startswith("/v1/"):
                await self._respond(writer, 204, {}, request_id, origin)
                return
            if path.startswith("/v1/") or path == "/v1":
                auth = headers.get("authorization", "")
                scheme, _, credential = auth.partition(" ")
                if scheme.lower() != "bearer" or not hmac.compare_digest(credential.encode(), self.token.encode()):
                    raise HTTPError(401, "A valid bearer token is required")
            if method == "GET" and path == "/healthz":
                body = {"status": "ok", "model_loaded": self.model_getter() is not None,
                        "uptime_s": int(time.monotonic() - self.started), "version": self.version,
                        **memory_telemetry()}
                await self._respond(writer, 200, body, request_id, origin)
            elif method == "GET" and path == "/v1/models":
                data = [] if self.model_getter() is None else [{"id": MODEL_ID, "object": "model", "owned_by": "scribekit"}]
                await self._respond(writer, 200, {"object": "list", "data": data}, request_id, origin)
            elif method == "POST" and path == "/v1/audio/transcriptions":
                length = headers.get("content-length", "0")
                if length.isascii() and length.isdigit() and int(length) > MAX_BODY:
                    raise HTTPError(413, "Upload exceeds 100 MB")
                if self.model_getter() is None:
                    raise HTTPError(503, "Speech model is not loaded; use dictation to load it")
                if self.jobs >= MAX_JOBS:
                    raise HTTPError(429, "Transcription queue is full; retry shortly")
                self.jobs += 1
                admitted = True
                directory = tempfile.TemporaryDirectory(prefix="scribekit-stt-")
                upload = MultipartUpload(headers.get("content-type", ""), Path(directory.name))
                if headers.get("expect", "").lower() == "100-continue":
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                await asyncio.wait_for(self._body(reader, headers, upload), 120)
                processing = asyncio.create_task(self._transcribe(upload, Path(directory.name), cancelled))
                disconnected = asyncio.create_task(reader.read(1))
                done, _ = await asyncio.wait((processing, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    # Only EOF means the peer is gone; a pipelined or stray byte
                    # is not a disconnect, and the connection closes after one
                    # response so the consumed byte cannot corrupt a later one.
                    # The job still finishes: a client that half-closed its write
                    # side is still reading, and a fully closed peer just fails
                    # the write and cleans up below. A vanished peer's cost is
                    # bounded by MAX_BODY (about 26 minutes of audio).
                    body, queue_ms = await processing
                    try:
                        await self._respond(writer, 200, body, request_id, origin, queue_ms)
                    except (OSError, asyncio.TimeoutError):
                        pass
                    return
                body, queue_ms = processing.result()
                await self._respond(writer, 200, body, request_id, origin, queue_ms)
            else:
                raise HTTPError(404, "Endpoint not found")
        except asyncio.CancelledError:
            try:
                await self._respond(writer, 503, error_body(503, "Localhost API is stopping"), request_id, origin)
            except (OSError, asyncio.TimeoutError):
                pass
        except (HTTPError, AudioError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ValueError) as exc:
            status = exc.status if isinstance(exc, HTTPError) else 408 if isinstance(exc, asyncio.TimeoutError) else 400
            message = exc.message if isinstance(exc, HTTPError) else str(exc) if isinstance(exc, AudioError) else "Invalid or incomplete request"
            try:
                await self._respond(writer, status, error_body(status, message, getattr(exc, "code", None)), request_id, origin)
            except (OSError, asyncio.TimeoutError):
                pass
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            # Never leak decoder exceptions, filenames, audio or transcript text.
            try:
                await self._respond(writer, 500, error_body(500, "Transcription failed"), request_id, origin)
            except (OSError, asyncio.TimeoutError):
                pass
        finally:
            cancelled.set()
            for child in (processing, disconnected):
                if child:
                    child.cancel()
            if upload:
                upload.close()
            if directory:
                directory.cleanup()
            if admitted:
                self.jobs -= 1
            self.clients.discard(task)
            writer.close()
