"""TOTP drift window and replay store."""

import base64
import hashlib
import hmac
import secrets as _secrets
import struct
import threading
import urllib.parse


class TotpError(Exception):
    """Error carrying a stable TOTP error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


_ISSUER = "FinanceVPN"


def _decode_secret(secret):
    if isinstance(secret, bytes):
        secret = secret.decode()
    padding = "=" * (-len(secret) % 8)
    return base64.b32decode(secret + padding)


class TotpDriftWindow:
    """Per-user TOTP secrets, drift and single-use counters."""

    def __init__(self, step=30, window=1, digits=6, max_failures=5):
        self.step = step
        self.window = window
        self.digits = digits
        self.max_failures = max_failures
        self._lock = threading.Lock()
        self._users = {}
        self._log = []

    @staticmethod
    def _new_record():
        return {
            "secret": None,
            "algorithm": "SHA1",
            "secret_version": 0,
            "device": None,
            "drift": 0,
            "last_counter": -1,
            "failures": 0,
            "backup_codes": set(),
            "used_backup_codes": set(),
        }

    def _hotp(self, secret, counter, algorithm):
        digest = hmac.new(secret, struct.pack(">Q", counter),
                          getattr(hashlib, algorithm.lower())).digest()
        offset = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(value % (10 ** self.digits)).zfill(self.digits)

    def enroll(self, user, secret, algorithm="SHA1", device=None):
        """Enroll a user secret.

        The same secret must not be shared by two users; otherwise raise
        TotpError("TOTP_SECRET_REUSE").  Returns {"enrolled": True,
        "secret_version": int}.
        """
        raw = _decode_secret(secret)
        algorithm = algorithm.upper()
        with self._lock:
            for other, record in self._users.items():
                if other != user and record["secret"] == raw:
                    raise TotpError("TOTP_SECRET_REUSE",
                                    "secret already enrolled by another user")
            record = self._users.get(user)
            if record is None:
                record = self._new_record()
                self._users[user] = record
            if record["secret"] != raw or record["algorithm"] != algorithm:
                record["secret_version"] += 1
                record["last_counter"] = -1
            record["secret"] = raw
            record["algorithm"] = algorithm
            record["device"] = device
            self._log.append("enroll user=%s version=%d device=%s" % (
                user, record["secret_version"], device))
            return {"enrolled": True, "secret_version": record["secret_version"]}

    def set_drift(self, user, seconds):
        """Record a per-user clock drift in seconds."""
        with self._lock:
            record = self._users.get(user)
            if record is None:
                raise TotpError("TOTP_UNKNOWN_USER", user)
            record["drift"] = int(seconds)

    def verify(self, user, code, timestamp, algorithm=None,
               source_ip="198.51.100.1"):
        """Verify a TOTP code with a small drift window.

        Returns {"ok": True, "counter": int, "last_counter": int}.  A code
        outside +/- ``window`` steps raises "TOTP_WINDOW"; a code at or below
        the last accepted counter is "TOTP_REPLAY"; a supplied algorithm that
        differs from enrollment is "TOTP_ALGORITHM_MISMATCH"; too many failures
        lock the account with "TOTP_LOCKED" regardless of source IP.
        """
        with self._lock:
            record = self._users.get(user)
            if record is None or record["secret"] is None:
                raise TotpError("TOTP_UNKNOWN_USER", user)
            if record["failures"] >= self.max_failures:
                raise TotpError("TOTP_LOCKED",
                                "account locked after %d failures" %
                                record["failures"])
            if algorithm is not None and \
                    algorithm.upper() != record["algorithm"]:
                record["failures"] += 1
                raise TotpError("TOTP_ALGORITHM_MISMATCH",
                                "enrolled=%s supplied=%s" %
                                (record["algorithm"], algorithm))
            counter = int((timestamp + record["drift"]) // self.step)
            matched = None
            for candidate in range(counter - self.window,
                                   counter + self.window + 1):
                if candidate < 0:
                    continue
                if hmac.compare_digest(
                        self._hotp(record["secret"], candidate,
                                   record["algorithm"]),
                        str(code)):
                    matched = candidate
                    break
            if matched is None:
                record["failures"] += 1
                raise TotpError("TOTP_WINDOW",
                                "code outside +/-%d steps" % self.window)
            if matched <= record["last_counter"]:
                raise TotpError("TOTP_REPLAY",
                                "counter %d already used" % matched)
            record["last_counter"] = matched
            record["failures"] = 0
            return {"ok": True, "counter": matched,
                    "last_counter": record["last_counter"]}

    def generate_backup_codes(self, user, count=3):
        """Generate one-time backup codes; returns {"codes": list}."""
        with self._lock:
            record = self._users.get(user)
            if record is None:
                raise TotpError("TOTP_UNKNOWN_USER", user)
            codes = [_secrets.token_hex(4) for _ in range(count)]
            record["backup_codes"].update(codes)
            return {"codes": codes}

    def use_backup_code(self, user, code):
        """Consume a backup code once; reuse raises "TOTP_REPLAY"."""
        with self._lock:
            record = self._users.get(user)
            if record is None:
                raise TotpError("TOTP_UNKNOWN_USER", user)
            if code in record["used_backup_codes"]:
                raise TotpError("TOTP_REPLAY", "backup code already used")
            if code not in record["backup_codes"]:
                raise TotpError("TOTP_BACKUP_INVALID", "unknown backup code")
            record["backup_codes"].discard(code)
            record["used_backup_codes"].add(code)
            return {"used": True}

    def provisioning_uri(self, user):
        """Return the otpauth provisioning URI for a user."""
        with self._lock:
            record = self._users.get(user)
            if record is None or record["secret"] is None:
                raise TotpError("TOTP_UNKNOWN_USER", user)
            secret_b32 = base64.b32encode(record["secret"]).decode()
            label = urllib.parse.quote("%s:%s" % (_ISSUER, user))
            query = urllib.parse.urlencode({
                "secret": secret_b32,
                "issuer": _ISSUER,
                "algorithm": record["algorithm"],
                "digits": self.digits,
                "period": self.step,
            })
            self._log.append("provisioning_uri user=%s version=%d" % (
                user, record["secret_version"]))
            return "otpauth://totp/%s?%s" % (label, query)

    def access_log(self):
        """Return log lines; secrets must never appear here."""
        with self._lock:
            return list(self._log)

    def dump_state(self):
        """Serialize last counters, secrets and failure counts."""
        with self._lock:
            return {
                "step": self.step,
                "window": self.window,
                "digits": self.digits,
                "max_failures": self.max_failures,
                "users": {
                    user: {
                        "secret": base64.b32encode(record["secret"]).decode()
                        if record["secret"] is not None else None,
                        "algorithm": record["algorithm"],
                        "secret_version": record["secret_version"],
                        "device": record["device"],
                        "drift": record["drift"],
                        "last_counter": record["last_counter"],
                        "failures": record["failures"],
                        "backup_codes": sorted(record["backup_codes"]),
                        "used_backup_codes": sorted(
                            record["used_backup_codes"]),
                    }
                    for user, record in self._users.items()
                },
            }

    def load_state(self, state):
        """Restore per-user counters after a restart."""
        with self._lock:
            self._users = {}
            for user, saved in state.get("users", {}).items():
                record = self._new_record()
                secret = saved.get("secret")
                record["secret"] = _decode_secret(secret) if secret else None
                record["algorithm"] = saved.get("algorithm", "SHA1")
                record["secret_version"] = saved.get("secret_version", 0)
                record["device"] = saved.get("device")
                record["drift"] = saved.get("drift", 0)
                record["last_counter"] = saved.get("last_counter", -1)
                record["failures"] = saved.get("failures", 0)
                record["backup_codes"] = set(saved.get("backup_codes", []))
                record["used_backup_codes"] = set(
                    saved.get("used_backup_codes", []))
                self._users[user] = record
