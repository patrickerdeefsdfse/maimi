"""Private, bounded gateway metadata producers. No request-body ingestion API.

Call configure once from gateway startup with its private MetadataStore. Hooks
receive a previously validated TrustedActor. They never retain prompt, reply,
filename, URL, raw error, credential, or provider model identifier.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import queue
import re
import threading
import time
from uuid import UUID, uuid4, uuid5

from .desktop_auth import TrustedActor

KINDS = frozenset({'model', 'task', 'office'})
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
USER = re.compile(r'^M[0-9]{8}$')
NAMESPACE = UUID('8f676644-3356-4e43-9cf9-143a42809c6f')
_sink = None


def owner(actor):
    # Type is necessary but not sufficient: hooks are ONLY placed after actual
    # actor validation, not at an HTTP body/header boundary.
    return actor.user_no if isinstance(actor, TrustedActor) and USER.fullmatch(actor.user_no) else None


class Dispatcher:
    """Bounded in-memory handoff; SQLite/fsync never runs on the request loop."""
    def __init__(self, store, capacity=1024):
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError('INVALID_QUEUE_LIMIT')
        self.store = store
        self.queue = queue.Queue(capacity)
        self.losses = {}
        self.lock = threading.RLock()
        self.closed = False
        for kind in KINDS:
            store.enable_source(kind)
        self.thread = threading.Thread(target=self._run, name='maimi-usage-metadata', daemon=True)
        self.thread.start()

    def _loss(self, kind, stamp):
        with self.lock:
            self.losses[kind] = max(stamp, self.losses.get(kind, 0))

    def submit(self, principal, event, *, observed=False):
        with self.lock:
            if self.closed:
                self._loss(event['kind'], event['completed_at'])
                return False
            try:
                self.queue.put_nowait((principal, event, observed))
                return True
            except queue.Full:
                self._loss(event['kind'], event['completed_at'])
                return False

    def _report_losses(self):
        with self.lock:
            pending, self.losses = self.losses, {}
        for kind, stamp in pending.items():
            try:
                self.store.record_loss(kind, stamp)
            except Exception:
                self._loss(kind, stamp)

    def _run(self):
        while not self.closed or not self.queue.empty():
            self._report_losses()
            try:
                principal, event, observed = self.queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                method = self.store.record_observed_event if observed else self.store.record_event
                method(trusted_owner=principal, event=event)
            except Exception:
                self._loss(event['kind'], event['completed_at'])
            finally:
                self.queue.task_done()
        self._report_losses()

    def drain(self, timeout=2):
        """Shutdown/tests only; never called by a request or stream chunk."""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.002)
        return self.queue.unfinished_tasks == 0

    def close(self, timeout=2):
        with self.lock:
            self.closed = True
        self.thread.join(timeout)
        return not self.thread.is_alive()


def configure(store, *, capacity=1024):
    global _sink
    if _sink is not None:
        raise RuntimeError('USAGE_PRODUCER_ALREADY_CONFIGURED')
    _sink = Dispatcher(store, capacity)
    return _sink


def shutdown():
    global _sink
    current, _sink = _sink, None
    _office.clear()
    return current.close() if current else True


def token(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def usage(value):
    """Provider-reported totals only. Cache misses are NOT cache writes."""
    result = dict.fromkeys(('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'))
    if not isinstance(value, dict):
        return result
    result['input_tokens'] = token(value.get('prompt_tokens'))
    result['output_tokens'] = token(value.get('completion_tokens'))
    details = value.get('prompt_tokens_details')
    read = token(details.get('cached_tokens')) if isinstance(details, dict) else None
    hit = token(value.get('prompt_cache_hit_tokens'))
    if read is None:
        read = hit
    elif hit is not None and read != hit:
        read = None
    if read is not None and result['input_tokens'] is not None and read > result['input_tokens']:
        read = None
    result['cache_read_tokens'] = read
    # No currently supported Chat Completions field establishes cache writes.
    return result


class SSEUsage:
    """A 64KiB maximum event tap. It cannot alter the forwarded stream bytes."""
    LIMIT = 65536

    def __init__(self):
        self.line = bytearray()
        self.data = bytearray()
        self.discard_line = False
        self.discard_event = False
        self.invalid = False
        self.done = False
        self.failed = False
        self.tokens = usage(None)

    def feed(self, chunk):
        for part in chunk.splitlines(keepends=True):
            end = part.endswith(b'\n')
            if not self.discard_line:
                if len(self.line) + len(part) > self.LIMIT:
                    self.line.clear()
                    self.discard_line = self.discard_event = self.invalid = True
                else:
                    self.line.extend(part)
            if not end:
                continue
            if not self.discard_line:
                line = bytes(self.line).rstrip(b'\r\n')
                if not line:
                    self._event()
                elif line.startswith(b'data:') and not self.discard_event:
                    value = line[5:]
                    if value.startswith(b' '):
                        value = value[1:]
                    if len(self.data) + len(value) + 1 > self.LIMIT:
                        self.data.clear()
                        self.discard_event = self.invalid = True
                    else:
                        if self.data:
                            self.data.extend(b'\n')
                        self.data.extend(value)
            self.line.clear()
            self.discard_line = False

    def _event(self):
        raw = bytes(self.data)
        self.data.clear()
        if self.discard_event:
            self.discard_event = False
            return
        if not raw:
            return
        if self.done:
            self.invalid = True
            return
        if raw == b'[DONE]':
            self.done = True
            return
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                self.invalid = True
                return
            if value.get('error') is not None:
                self.failed = True
            if 'usage' in value and value['usage'] is not None:
                self.tokens = usage(value['usage'])
        except (ValueError, UnicodeError, RecursionError):
            self.invalid = True

    def status(self):
        if self.failed:
            return 'failed'
        return 'succeeded' if self.done and not self.invalid and not self.line and not self.data else 'unknown'

    def clear(self):
        self.line.clear()
        self.data.clear()


class ModelMeter:
    def __init__(self, principal, sink):
        self.principal, self.sink = principal, sink
        self.id = str(uuid4())
        self.started = time.monotonic()
        self.parser = SSEUsage()
        self.finished = False

    def feed(self, chunk):
        if not self.finished:
            self.parser.feed(chunk)

    def finish(self, status=None, reported_usage=None):
        if self.finished:
            return
        self.finished = True
        tokens = usage(reported_usage) if reported_usage is not None else self.parser.tokens
        event = {'event_id': self.id, 'kind': 'model', 'completed_at': time.time(),
                 'status': status or self.parser.status(),
                 'duration_ms': max(0, int((time.monotonic() - self.started) * 1000)), **tokens}
        self.parser.clear()
        self.sink.submit(self.principal, event)


def model(actor):
    principal = owner(actor)
    return ModelMeter(principal, _sink) if principal and _sink else None


def _event(actor, kind, job, status, started=None):
    principal = owner(actor)
    if not principal or not _sink or status not in {'succeeded', 'failed', 'cancelled', 'unknown'}:
        return
    # The identifier is a deterministic opaque UUID, not a retained job/session.
    event_id = str(uuid5(NAMESPACE, kind + ':' + principal + ':' + actor.auth_session_id + ':' + job))
    event = {'event_id': event_id, 'kind': kind, 'completed_at': time.time(), 'status': status,
             'duration_ms': max(0, int((time.monotonic() - started) * 1000)) if started is not None else None,
             **usage(None)}
    _sink.submit(principal, event, observed=True)


_office = OrderedDict()


def office_started(actor, job, started):
    if not _sink or not owner(actor):
        return
    key = (actor.user_no, actor.auth_session_id, job)
    now = time.monotonic()
    for old, when in list(_office.items()):
        if now - when > 3600:
            _office.pop(old, None)
    if key not in _office:
        if len(_office) >= 4096:
            _office.popitem(last=False)
        _office[key] = started


def office_observed(actor, job, state):
    if state not in TERMINAL:
        return
    started = _office.pop((actor.user_no, actor.auth_session_id, job), None)
    if started is not None and time.monotonic() - started > 3600:
        started = None
    _event(actor, 'office', job, state, started)


def task_finished(actor, job, saved_status, started, *, remote_unknown=False):
    state = {'completed': 'succeeded', 'failed': 'failed', 'cancelled': 'cancelled', 'interrupted': 'unknown'}.get(saved_status)
    if state:
        _event(actor, 'task', job, 'unknown' if remote_unknown else state, started)
