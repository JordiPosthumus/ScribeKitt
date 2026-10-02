"""One model worker; dictation always precedes waiting HTTP chunks.

The condition sleeps indefinitely when idle. The 500 ms timer is armed only
when an API chunk is waiting, and never performs speculative inference.
"""
from collections import deque
from concurrent.futures import Future
import threading
import time


class InferenceScheduler:
    def __init__(self, idle_seconds=0.5):
        self.idle_seconds = idle_seconds
        self.condition = threading.Condition()
        self.dictation = deque()
        self.api = deque()
        self.last_dictation = time.monotonic()
        self.recording = False
        self.closed = False
        self.worker = threading.Thread(target=self._run, name="ScribeKitt-inference", daemon=True)
        self.worker.start()

    def set_recording(self, active):
        with self.condition:
            self.recording = bool(active)
            self.last_dictation = time.monotonic()
            self.condition.notify_all()

    def submit(self, function, *, api=False):
        future = Future()
        with self.condition:
            if self.closed:
                future.set_exception(RuntimeError("Inference worker stopped"))
            else:
                queue = self.api if api else self.dictation
                item = (future, function, time.monotonic())
                queue.append(item)
                def discard_cancelled(done):
                    if done.cancelled():
                        with self.condition:
                            try:
                                queue.remove(item)
                            except ValueError:
                                pass
                future.add_done_callback(discard_cancelled)
                self.condition.notify_all()
        return future

    def close(self):
        # Never join a model call during app quit.
        with self.condition:
            self.closed = True
            for queue in (self.dictation, self.api):
                while queue:
                    queue.popleft()[0].cancel()
            self.condition.notify_all()

    def _run(self):
        while True:
            with self.condition:
                while True:
                    if self.closed:
                        return
                    if self.dictation:
                        item, is_api = self.dictation.popleft(), False
                        break
                    if self.api and not self.recording:
                        delay = self.idle_seconds - (time.monotonic() - self.last_dictation)
                        if delay <= 0:
                            item, is_api = self.api.popleft(), True
                            break
                        self.condition.wait(delay)
                    else:
                        self.condition.wait()
            future, function, queued_at = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                waited_ms = (time.monotonic() - queued_at) * 1000
                result = function()
                future.set_result((result, waited_ms) if is_api else result)
            except Exception as exc:
                future.set_exception(exc)
            finally:
                if not is_api:
                    with self.condition:
                        self.last_dictation = time.monotonic()
                        self.condition.notify_all()
