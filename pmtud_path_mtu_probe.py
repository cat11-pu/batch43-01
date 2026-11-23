"""Path MTU discovery probe and per-path PMTU cache."""

import threading


IPV6_MIN_MTU = 1280
TCP_IP_HEADER_OVERHEAD = 40
MIN_QUOTED_BYTES = 8


class PmtudError(Exception):
    """Cache or configuration failure raised by the probe."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def _is_ipv6(address):
    return isinstance(address, str) and ":" in address


class PmtudPathMtuProbe:
    """Verifies ICMP PMTU reports and maintains per-path MTU values.

    Cache entries are keyed by (destination, tunnel) so that paths with
    different encapsulation overhead never share a value, and every mutation
    happens under a lock so concurrent reports cannot clobber each other.
    """

    def __init__(self, default_mtu=1500, cache_ttl=60.0, verify_window=30.0):
        self.default_mtu = default_mtu
        self.cache_ttl = cache_ttl
        self.verify_window = verify_window
        self._lock = threading.RLock()
        # (dest, tunnel) -> {"mtu": int, "ts": float}
        self._cache = {}
        # (flow, dest, tunnel) -> list of (size, ts, df, inner_df)
        self._sent = {}

    def _key(self, dest, tunnel):
        return dest, tunnel

    def _live_entry(self, key, now):
        """Return a non-expired cache entry or None, pruning stale ones."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        if now - entry["ts"] > self.cache_ttl:
            del self._cache[key]
            return None
        return entry

    def _prune_sent(self, now):
        deadline = now - self.verify_window
        for log_key in list(self._sent):
            records = [record for record in self._sent[log_key] if record[1] >= deadline]
            if records:
                self._sent[log_key] = records
            else:
                del self._sent[log_key]

    def send(self, flow, dest, size, tunnel=None, df=True, inner_df=True, now=0.0):
        key = self._key(dest, tunnel)
        with self._lock:
            self._prune_sent(now)
            entry = self._live_entry(key, now)
            path_mtu = entry["mtu"] if entry is not None else self.default_mtu

            if size > path_mtu and df:
                # DF set: the packet cannot be fragmented, so drop it and
                # emit a fragmentation-needed control message instead of
                # black-holing it silently.
                return {
                    "action": "fragmentation_needed",
                    "code": "PMTU_FRAG_NEEDED",
                    "icmp_sent": True,
                    "fragmented": False,
                    "mtu": path_mtu,
                }

            fragmented = size > path_mtu
            # An inner packet carrying DF must never be fragmented by the
            # outer encapsulation of a tunnel; honor it by dropping and
            # emitting a control message. Without a tunnel the outer DF
            # flag is the packet's own, so df=False permits fragmentation.
            if fragmented and tunnel is not None and inner_df:
                return {
                    "action": "fragmentation_needed",
                    "code": "PMTU_FRAG_NEEDED",
                    "icmp_sent": True,
                    "fragmented": False,
                    "mtu": path_mtu,
                }

            self._sent.setdefault((flow, dest, tunnel), []).append(
                (size, now, df, inner_df)
            )
            return {
                "action": "send",
                "code": None,
                "icmp_sent": False,
                "fragmented": fragmented,
                "mtu": path_mtu,
            }

    def on_icmp(
        self,
        flow,
        dest,
        tunnel=None,
        reported_mtu=1280,
        quoted_bytes=8,
        icmp_type=3,
        icmp_code=4,
        now=0.0,
    ):
        ipv6 = _is_ipv6(dest)
        expected_type = 2 if ipv6 else 3
        expected_code = 0 if ipv6 else 4
        if icmp_type != expected_type or icmp_code != expected_code:
            return {
                "accepted": False,
                "code": "PMTU_ICMP_IGNORED",
                "mtu": self.mtu_for(dest, tunnel=tunnel, now=now),
            }

        with self._lock:
            self._prune_sent(now)
            records = self._sent.get((flow, dest, tunnel), [])
            has_recent_match = bool(records)

            # The control message must quote at least 8 bytes and must
            # correlate with a packet we actually sent recently, matching
            # the inner five-tuple, destination and tunnel.
            if quoted_bytes < MIN_QUOTED_BYTES or not has_recent_match:
                return {
                    "accepted": False,
                    "code": "PMTU_ICMP_UNVERIFIED",
                    "mtu": self.mtu_for(dest, tunnel=tunnel, now=now),
                }

            new_mtu = reported_mtu
            if ipv6 and new_mtu < IPV6_MIN_MTU:
                new_mtu = IPV6_MIN_MTU

            key = self._key(dest, tunnel)
            entry = self._live_entry(key, now)
            current_mtu = entry["mtu"] if entry is not None else self.default_mtu
            # PMTU can only be lowered by a verified report; it recovers
            # through aging or route invalidation, never via a stale report.
            effective_mtu = min(current_mtu, new_mtu)
            self._cache[key] = {"mtu": effective_mtu, "ts": now}
            return {
                "accepted": True,
                "code": None,
                "mtu": effective_mtu,
            }

    def mtu_for(self, dest, tunnel=None, now=0.0):
        key = self._key(dest, tunnel)
        with self._lock:
            entry = self._live_entry(key, now)
            if entry is None:
                return self.default_mtu
            return entry["mtu"]

    def clamp_mss(self, dest, mss, tunnel=None, direction="out", now=0.0):
        path_mtu = self.mtu_for(dest, tunnel=tunnel, now=now)
        mss_clamp = path_mtu - TCP_IP_HEADER_OVERHEAD
        # Clamping applies symmetrically to SYN and SYN/ACK segments.
        return min(mss, mss_clamp)

    def route_change(self, dest):
        with self._lock:
            # Atomic invalidation: remove every tunnel entry for the dest
            # under the lock so no packet can read a stale value.
            for key in [key for key in self._cache if key[0] == dest]:
                del self._cache[key]
            for log_key in [log_key for log_key in self._sent if log_key[1] == dest]:
                del self._sent[log_key]

    def snapshot(self):
        with self._lock:
            return {
                "version": 1,
                "entries": [
                    {
                        "dest": dest,
                        "tunnel": tunnel,
                        "mtu": entry["mtu"],
                        "ts": entry["ts"],
                    }
                    for (dest, tunnel), entry in self._cache.items()
                ],
            }

    def restore(self, state, now=0.0):
        with self._lock:
            self._cache.clear()
            self._sent.clear()
            for item in state.get("entries", []):
                ts = item["ts"]
                # Values older than the TTL belong to a previous path
                # incarnation and must not be reused after a restart.
                if now - ts > self.cache_ttl:
                    continue
                dest = item["dest"]
                tunnel = item.get("tunnel")
                self._cache[self._key(dest, tunnel)] = {
                    "mtu": item["mtu"],
                    "ts": ts,
                }

    def report_application_timeout(self, dest):
        # Application-layer timeouts are not evidence of an MTU black hole
        # and must never drive the cached path MTU downward.
        return {
            "action": "ignored",
            "code": "PMTU_APP_TIMEOUT_IGNORED",
            "dest": dest,
        }
