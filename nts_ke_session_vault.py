"""NTS-KE session vault and authenticated time sync.

The vault owns NTS-KE credentials and refuses to fall back to
unauthenticated time protocols. Failure modes are contained:

* certificate / expected-hostname mismatch -> NTS_KE_TLS_INVALID
* negotiated algorithm outside the allowlist -> NTS_AEAD_UNSUPPORTED
* replayed nonce -> NTS_NONCE_REPLAY (clock is never adjusted)
* mismatched unique identifier -> NTS_UNIQUE_ID_MISMATCH
* expired or revoked credential -> NTS_CREDENTIAL_EXPIRED
* empty vault -> polling stops, never downgrades to plain NTP
"""

import json
import os
import threading


class NtsError(Exception):
    """Key-establishment or credential failure raised by the vault."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class NtsKeSessionVault:
    """Owns NTS-KE credentials and refuses unauthenticated time requests."""

    def __init__(self, allowed_algorithms=("AEAD_AES_SIV_CMAC_256",), state_path=None):
        self.allowed_algorithms = tuple(allowed_algorithms)
        self.state_path = state_path
        self._lock = threading.RLock()
        self._credentials = {}
        self._failed_servers = set()
        self._offset = 0.0
        self._counters = {
            "plain_ntp_requests": 0,
            "tls_failures": 0,
            "unsupported_algorithms": 0,
            "nonce_replays_rejected": 0,
            "unique_id_mismatches": 0,
            "expired_credential_uses": 0,
        }
        self._load()

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return
        for server, record in state.get("credentials", {}).items():
            record = dict(record)
            record["used_nonces"] = set(record.get("used_nonces", []))
            self._credentials[server] = record

    def _save(self):
        if not self.state_path:
            return
        state = {"credentials": {}}
        for server, record in self._credentials.items():
            stored = dict(record)
            stored["used_nonces"] = sorted(record["used_nonces"])
            state["credentials"][server] = stored
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_path, self.state_path)

    def _valid_credential(self, server, now):
        record = self._credentials.get(server)
        if record is None or record.get("revoked"):
            return None
        if now >= record["expires_at"]:
            return None
        return record

    def establish(
        self,
        server,
        expected_host,
        cert_cn,
        algorithm="AEAD_AES_SIV_CMAC_256",
        nonce=None,
        lifetime=3600.0,
        now=0.0,
    ):
        with self._lock:
            if cert_cn != expected_host or expected_host != server:
                self._counters["tls_failures"] += 1
                self._failed_servers.add(server)
                raise NtsError(
                    "NTS_KE_TLS_INVALID",
                    "certificate name does not match expected host",
                )
            if algorithm not in self.allowed_algorithms:
                self._counters["unsupported_algorithms"] += 1
                self._failed_servers.add(server)
                raise NtsError(
                    "NTS_AEAD_UNSUPPORTED",
                    "negotiated algorithm is not in the allowlist",
                )
            record = {
                "server": server,
                "algorithm": algorithm,
                "authenticated_at": now,
                "expires_at": now + lifetime,
                "used_nonces": set(),
                "unique_id": None,
                "revoked": False,
            }
            self._credentials[server] = record
            self._failed_servers.discard(server)
            self._save()
            return {
                "server": record["server"],
                "algorithm": record["algorithm"],
                "authenticated_at": record["authenticated_at"],
                "expires_at": record["expires_at"],
            }

    def sync(self, server, nonce, unique_id, now=0.0, server_time=None):
        with self._lock:
            record = self._credentials.get(server)
            if record is None:
                raise NtsError(
                    "NTS_KE_TLS_INVALID",
                    "no credential bound to this server identity",
                )
            if record.get("revoked") or now >= record["expires_at"]:
                self._counters["expired_credential_uses"] += 1
                raise NtsError(
                    "NTS_CREDENTIAL_EXPIRED",
                    "credential is expired or revoked",
                )
            if nonce in record["used_nonces"]:
                self._counters["nonce_replays_rejected"] += 1
                raise NtsError("NTS_NONCE_REPLAY", "replayed nonce rejected")
            if record["unique_id"] is not None and record["unique_id"] != unique_id:
                self._counters["unique_id_mismatches"] += 1
                raise NtsError(
                    "NTS_UNIQUE_ID_MISMATCH",
                    "unique identifier does not match the established session",
                )
            record["used_nonces"].add(nonce)
            record["unique_id"] = unique_id
            if server_time is not None:
                self._offset = server_time - now
            self._save()
            return {"ok": True, "offset": self._offset, "server": server}

    def credentials(self, server, now=0.0):
        with self._lock:
            record = self._valid_credential(server, now)
            if record is None:
                return None
            return {
                "server": record["server"],
                "algorithm": record["algorithm"],
                "authenticated_at": record["authenticated_at"],
                "expires_at": record["expires_at"],
            }

    def poll(self, server, now=0.0):
        with self._lock:
            if server in self._failed_servers:
                return {"action": "stop", "server": server, "code": "HANDSHAKE_FAILED"}
            record = self._credentials.get(server)
            if record is None:
                return {"action": "stop", "server": server, "code": "NO_CREDENTIAL"}
            if record.get("revoked") or now >= record["expires_at"]:
                return {
                    "action": "stop",
                    "server": server,
                    "code": "NTS_CREDENTIAL_EXPIRED",
                }
            return {
                "action": "sync",
                "server": server,
                "algorithm": record["algorithm"],
            }

    def rotate_master_key(self, server):
        with self._lock:
            record = self._credentials.get(server)
            if record is not None:
                record["revoked"] = True
                self._save()

    def clock(self):
        with self._lock:
            return self._offset

    def counters(self):
        with self._lock:
            return dict(self._counters)
