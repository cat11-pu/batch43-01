"""CIBA consent transcript and single-token issuance."""

import json
import os
import threading


class CibaError(Exception):
    """Authentication-request failure raised by the transcript."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


_PENDING = "pending"
_APPROVED = "approved"
_DENIED = "denied"


class CibaConsentTranscript:
    """Runs the CIBA request state machine with poll and callback delivery."""

    def __init__(self, poll_interval=5.0, request_ttl=600.0, state_path=None):
        self.poll_interval = poll_interval
        self.request_ttl = request_ttl
        self.state_path = state_path
        self._lock = threading.Lock()
        self._requests = {}
        self._records = []
        self._token_seq = 0
        if state_path and os.path.exists(state_path):
            self._load()

    def _load(self):
        with open(self.state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._requests = data.get("requests", {})
        self._records = data.get("records", [])
        self._token_seq = data.get("token_seq", 0)

    def _save(self):
        if not self.state_path:
            return
        data = {
            "requests": self._requests,
            "records": self._records,
            "token_seq": self._token_seq,
        }
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_path, self.state_path)

    def start(self, request_id, client_id, scope="openid", ttl=None, now=0.0):
        with self._lock:
            if request_id in self._requests:
                raise CibaError(
                    "invalid_request", "auth request id already registered"
                )
            self._requests[request_id] = {
                "request_id": request_id,
                "client_id": client_id,
                "scope": scope,
                "status": _PENDING,
                "created_at": now,
                "ttl": ttl if ttl is not None else self.request_ttl,
                "interval": self.poll_interval,
                "last_poll_at": None,
                "delivered": False,
                "delivered_via": None,
                "user": None,
            }
            self._save()
            return {"request_id": request_id, "status": _PENDING}

    def _get_for_client(self, request_id, client_id):
        request = self._requests.get(request_id)
        if request is None:
            raise CibaError("invalid_grant", "unknown auth request id")
        if request["client_id"] != client_id:
            raise CibaError(
                "unauthorized_client", "auth request bound to another client"
            )
        return request

    def _check_live(self, request, now):
        if now - request["created_at"] > request["ttl"]:
            raise CibaError("invalid_grant", "auth request expired")

    def _deliver(self, request, now, via):
        self._check_live(request, now)
        if request["status"] == _DENIED:
            raise CibaError("access_denied", "user denied the request")
        if request["status"] == _PENDING:
            return {"status": "authorization_pending"}
        if request["delivered"]:
            raise CibaError("invalid_grant", "token already issued")
        self._token_seq += 1
        token = "token-%s-%d" % (request["request_id"], self._token_seq)
        request["delivered"] = True
        request["delivered_via"] = via
        for record in self._records:
            if record["request_id"] == request["request_id"]:
                record["delivered_via"] = via
        self._save()
        return {"status": _APPROVED, "access_token": token}

    def poll(self, request_id, client_id, now=0.0):
        with self._lock:
            request = self._get_for_client(request_id, client_id)
            if request["status"] == _PENDING:
                self._check_live(request, now)
                last = request["last_poll_at"]
                if last is not None and now - last < request["interval"]:
                    request["interval"] += self.poll_interval
                    request["last_poll_at"] = now
                    self._save()
                    raise CibaError("slow_down", "polling too frequently")
                request["last_poll_at"] = now
                self._save()
            return self._deliver(request, now, via="poll")

    def callback(self, request_id, client_id, source_verified=True, now=0.0):
        with self._lock:
            if not source_verified:
                raise CibaError(
                    "unauthorized_client", "callback source not verified"
                )
            request = self._get_for_client(request_id, client_id)
            return self._deliver(request, now, via="callback")

    def approve(self, request_id, user="alice", now=0.0):
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise CibaError("invalid_grant", "unknown auth request id")
            if request["status"] == _DENIED:
                raise CibaError(
                    "invalid_grant", "denied request is terminal"
                )
            if request["status"] == _APPROVED:
                return {"request_id": request_id, "status": _APPROVED}
            self._check_live(request, now)
            request["status"] = _APPROVED
            request["user"] = user
            self._records.append(
                {
                    "request_id": request_id,
                    "client_id": request["client_id"],
                    "user": user,
                    "approved_at": now,
                    "delivered_via": None,
                }
            )
            self._save()
            return {"request_id": request_id, "status": _APPROVED}

    def deny(self, request_id, now=0.0):
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise CibaError("invalid_grant", "unknown auth request id")
            if request["status"] == _APPROVED:
                raise CibaError(
                    "invalid_grant", "approved request is terminal"
                )
            if request["status"] == _DENIED:
                return {"request_id": request_id, "status": _DENIED}
            request["status"] = _DENIED
            self._save()
            return {"request_id": request_id, "status": _DENIED}

    def records(self):
        with self._lock:
            return [dict(record) for record in self._records]
