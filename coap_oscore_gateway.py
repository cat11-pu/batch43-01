"""CoAP / OSCORE gateway replay, block and observation guard."""

import json
import os
import threading


class CoapError(Exception):
    """Protocol failure raised by the gateway."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CoapOscoreGateway:
    """Terminates OSCORE requests and enforces replay and block assembly rules."""

    MAX_REPLAY_WINDOW = 32

    def __init__(
        self,
        state_path=None,
        replay_window=16,
        max_token_len=8,
        group_key_id=None,
        max_skew=300,
    ):
        self.state_path = state_path
        self.replay_window = max(1, min(int(replay_window), self.MAX_REPLAY_WINDOW))
        self.max_token_len = max_token_len
        self.group_key_id = group_key_id
        self.max_skew = max_skew
        self._lock = threading.Lock()
        self._max_seq = {}
        self._counters = {
            "replay": 0,
            "stale": 0,
            "bad_token": 0,
            "bad_group_key": 0,
        }
        self._blocks = {}
        self._next_buffer_id = 0
        self._observations = {}
        self._notifications = {}
        self._load_state()

    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for sender_id, seq in data.get("senders", {}).items():
            self._max_seq[sender_id] = int(seq)

    def _save_state(self):
        if not self.state_path:
            return
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({"senders": self._max_seq}, fh)
        os.replace(tmp_path, self.state_path)

    def handle_request(
        self,
        endpoint,
        sender_id,
        partial_iv,
        token=b"t",
        content_format=0,
        multicast=False,
        group_key_id=None,
        timestamp=0.0,
        now=0.0,
        payload=b"",
    ):
        if not 1 <= len(token) <= self.max_token_len:
            self._counters["bad_token"] += 1
            return {"code": "4.00", "executed": False, "reason": "invalid token length"}
        if multicast and (
            self.group_key_id is None or group_key_id != self.group_key_id
        ):
            self._counters["bad_group_key"] += 1
            return {"code": "4.01", "executed": False, "reason": "group key required"}
        if abs(now - timestamp) > self.max_skew:
            self._counters["stale"] += 1
            return {"code": "4.01", "executed": False, "reason": "stale timestamp"}
        with self._lock:
            last_seen = self._max_seq.get(sender_id)
            if last_seen is not None and partial_iv <= last_seen:
                self._counters["replay"] += 1
                return {"code": "4.01", "executed": False, "reason": "replay"}
            self._max_seq[sender_id] = partial_iv
            self._save_state()
        return {"code": "2.05", "executed": True}

    def begin_block(self, endpoint, token, content_format=0):
        with self._lock:
            buffer_id = self._next_buffer_id
            self._next_buffer_id += 1
            self._blocks[buffer_id] = {
                "endpoint": endpoint,
                "token": token,
                "content_format": content_format,
                "chunks": [],
            }
        return buffer_id

    def add_block(self, buffer_id, payload, content_format=None):
        buffer = self._blocks.get(buffer_id)
        if buffer is None:
            return {"code": "4.04", "executed": False, "reason": "unknown buffer"}
        if content_format is not None and content_format != buffer["content_format"]:
            return {"code": "4.08", "executed": False, "reason": "block key mismatch"}
        buffer["chunks"].append(payload)
        return {
            "code": "2.04",
            "executed": True,
            "data": b"".join(buffer["chunks"]),
        }

    def observe(self, endpoint, token, resource):
        with self._lock:
            self._observations[token] = {"endpoint": endpoint, "resource": resource}
            self._notifications.setdefault(token, [])
        return token

    def cancel_observe(self, token):
        with self._lock:
            self._observations.pop(token, None)

    def reset(self, endpoint, token):
        with self._lock:
            registration = self._observations.get(token)
            if registration is not None and registration["endpoint"] == endpoint:
                del self._observations[token]

    def notify(self, token, payload):
        with self._lock:
            if token not in self._observations:
                return False
            self._notifications.setdefault(token, []).append(payload)
            return True

    def notifications(self, token):
        return list(self._notifications.get(token, []))

    def counters(self):
        return dict(self._counters)
