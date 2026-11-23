"""Small, process-local caches for shared report evidence."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
from threading import Lock
from time import monotonic


class SnapshotCache:
    def __init__(self, ttl, limit):
        self.ttl = ttl
        self.limit = limit
        self.entries = OrderedDict()
        self.pending = {}
        self.lock = Lock()

    def get_or_load(self, key, load, *, cache_if=bool):
        with self.lock:
            saved = self.entries.get(key)
            if saved and monotonic() - saved[0] < self.ttl:
                self.entries.move_to_end(key)
                return deepcopy(saved[1])
            pending = self.pending.get(key)
            if pending is None:
                self.pending[key] = future = Future()
        if pending is not None:
            return deepcopy(pending.result(timeout=180))
        try:
            result = load()
            snapshot = deepcopy(result)
            if cache_if(snapshot):
                with self.lock:
                    self.entries[key] = (monotonic(), snapshot)
                    self.entries.move_to_end(key)
                    if len(self.entries) > self.limit:
                        self.entries.popitem(last=False)
            future.set_result(snapshot)
            return result
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                del self.pending[key]
