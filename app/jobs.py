"""Laufende Vorgänge (Start, Klonen). Nur im Arbeitsspeicher."""

import logging
import queue
import threading
import time
import uuid

log = logging.getLogger(__name__)

# Abgeschlossene Jobs so lange aufheben (Anzeige in der UI)
KEEP_FINISHED = 24 * 3600

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"


class Busy(Exception):
    pass


class NotAwaiting(Exception):
    pass


class Step:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.status = PENDING
        self.detail = ""

    def to_dict(self):
        return {"key": self.key, "label": self.label, "status": self.status, "detail": self.detail}


class Job:
    TERMINAL = ("unlocked", "done", "failed")

    def __init__(self, kind, vm, steps, source=None):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.vm = vm
        self.source = source
        self.state = "queued"
        self.steps = [Step(k, label) for k, label in steps]
        self.error = None
        self.hint = None
        self.result = {}
        self.created = time.time()
        self.finished_at = None
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._awaiting = False
        self._await_count = 0  # wie oft schon nach einer Passphrase gefragt wurde
        self._retry = queue.Queue(maxsize=1)
        self._names = set()

    # --- Zustand ---------------------------------------------------------
    @property
    def finished(self):
        return self.state in self.TERMINAL

    def set_state(self, state):
        with self._lock:
            self.state = state

    def step(self, key):
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(key)

    def begin(self, key, detail=""):
        with self._lock:
            s = self.step(key)
            s.status, s.detail = RUNNING, detail

    def update(self, key, detail):
        with self._lock:
            self.step(key).detail = detail

    def complete(self, key, detail=None, status=DONE):
        with self._lock:
            s = self.step(key)
            s.status = status
            if detail is not None:
                s.detail = detail

    def fail(self, message, hint=None):
        with self._lock:
            for s in self.steps:
                if s.status == RUNNING:
                    s.status = FAILED
            self.error = message
            self.hint = hint
            self.state = "failed"
            self.finished_at = time.time()
            self._awaiting = False

    def finish(self, state):
        with self._lock:
            self.state = state
            self.finished_at = time.time()

    def set_result(self, **values):
        with self._lock:
            self.result.update(values)

    def cancel(self):
        self.cancel_event.set()

    # --- Erneute Passphrase-Eingabe -------------------------------------
    @property
    def awaiting_passphrase(self):
        return self._awaiting

    def wait_for_passphrase(self, timeout):
        """Blockiert bis provide_passphrase() oder Timeout/Abbruch (-> None)."""
        with self._lock:
            self._awaiting = True
            self._await_count += 1
        try:
            deadline = time.monotonic() + timeout
            while not self.cancel_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    return self._retry.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
            return None
        finally:
            with self._lock:
                self._awaiting = False

    def provide_passphrase(self, passphrase):
        with self._lock:
            if not self._awaiting:
                raise NotAwaiting()
            try:
                self._retry.put_nowait(passphrase)
            except queue.Full:
                raise NotAwaiting()
            self._awaiting = False

    def to_dict(self):
        # Enthält nie Passphrasen oder Konsolenausgabe.
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "vm": self.vm,
                "source": self.source,
                "state": self.state,
                "finished": self.finished,
                "awaiting_passphrase": self._awaiting,
                "passphrase_requests": self._await_count,
                "steps": [s.to_dict() for s in self.steps],
                "error": self.error,
                "hint": self.hint,
                "result": dict(self.result),
                "created": self.created,
                "finished_at": self.finished_at,
            }


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._active = {}  # VM-Name -> Job

    def submit(self, job, names, target):
        """Startet target(job) in einem Thread. names: VMs, die der Job sperrt."""
        with self._lock:
            self._prune()
            for n in names:
                if n in self._active:
                    raise Busy(n)
            for n in names:
                self._active[n] = job
            job._names = set(names)
            self._jobs[job.id] = job

        def run():
            try:
                target(job)
            except Exception:
                log.exception("Job %s (%s %s) abgestürzt", job.id, job.kind, job.vm)
                if not job.finished:
                    job.fail("Interner Fehler, Details im Log des Containers.")
            finally:
                self.release(job)

        threading.Thread(target=run, name=f"job-{job.kind}-{job.vm}", daemon=True).start()
        return job

    def release(self, job, name=None):
        """Gibt alle (oder eine) vom Job gesperrten VMs frei."""
        with self._lock:
            names = [name] if name else list(job._names)
            for n in names:
                if self._active.get(n) is job:
                    del self._active[n]
                job._names.discard(n)

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def active_for(self, name):
        with self._lock:
            return self._active.get(name)

    def all(self):
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created)

    def latest_for_target(self, name):
        """Letzter Job, dessen Ziel-VM name ist."""
        jobs = [j for j in self.all() if j.vm == name]
        return jobs[-1] if jobs else None

    def _prune(self):
        now = time.time()
        for jid, j in list(self._jobs.items()):
            keep_failed_clone = j.kind == "clone" and j.state == "failed" and j.result.get("deletable")
            if j.finished and j.finished_at and now - j.finished_at > KEEP_FINISHED and not keep_failed_clone:
                del self._jobs[jid]
