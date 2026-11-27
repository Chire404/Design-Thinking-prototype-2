"""Thread-safe Server-Sent Events (SSE) real-time notification hub."""

from __future__ import annotations

import json
import queue
import threading
from collections import defaultdict


class RealtimeBroker:
    """Publishes instant wallet, QR rotation, and system events to connected User/Admin tabs."""

    def __init__(self) -> None:
        self._user_queues: dict[int, set[queue.Queue]] = defaultdict(set)
        self._admin_queues: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe_user(self, user_id: int) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._user_queues[user_id].add(q)
        return q

    def unsubscribe_user(self, user_id: int, q: queue.Queue) -> None:
        with self._lock:
            qs = self._user_queues.get(user_id)
            if qs:
                qs.discard(q)
                if not qs:
                    self._user_queues.pop(user_id, None)

    def subscribe_admin(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._admin_queues.add(q)
        return q

    def unsubscribe_admin(self, q: queue.Queue) -> None:
        with self._lock:
            self._admin_queues.discard(q)

    def publish_to_user(self, user_id: int, event_type: str, payload: dict) -> None:
        message = self._format_sse(event_type, payload)
        with self._lock:
            targets = list(self._user_queues.get(user_id, ()))
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass

    def publish_to_admins(self, event_type: str, payload: dict) -> None:
        message = self._format_sse(event_type, payload)
        with self._lock:
            targets = list(self._admin_queues)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass

    @staticmethod
    def _format_sse(event_type: str, payload: dict) -> str:
        data = json.dumps(payload, ensure_ascii=False)
        return f"event: {event_type}\ndata: {data}\n\n"


realtime_broker = RealtimeBroker()
