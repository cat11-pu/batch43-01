"""DHCPv6 prefix delegation broker.

Leases are keyed by (normalized DUID, IAID); the relay link address is only
recorded for diagnostics and never participates in binding identity.  Every
mutation is serialized by a re-entrant lock so that concurrent solicitations
from the same CPE cannot create duplicate bindings, and the lease table is
persisted to JSON so that a restart rebuilds the free-prefix view instead of
re-issuing prefixes that are already delegated.
"""

import ipaddress
import json
import os
import threading


MIN_PREFIX_LENGTH = 48
MAX_PREFIX_LENGTH = 64


class Dhcpv6Error(Exception):
    """Binding or configuration failure raised by the broker."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def normalize_duid(duid):
    """Collapse DUID encodings to one identity (lowercase hex, no separators)."""

    if isinstance(duid, bytes):
        return duid.hex()
    return str(duid).strip().replace("-", "").replace(":", "").replace(".", "").lower()


class Dhcpv6PdBroker:
    """Allocates and tracks delegated IPv6 prefixes per DUID and IAID."""

    def __init__(
        self,
        pool="2001:db8::/48",
        prefix_length=56,
        state_path=None,
        valid_lifetime=100,
        preferred_lifetime=50,
        max_hops=2,
        capacity=None,
    ):
        self.pool = pool
        self.prefix_length = prefix_length
        self.state_path = state_path
        self.valid_lifetime = valid_lifetime
        self.preferred_lifetime = preferred_lifetime
        self.max_hops = max_hops
        self.capacity = capacity

        self._network = ipaddress.IPv6Network(pool, strict=False)
        self._lock = threading.RLock()
        self._bindings = {}
        self._allocated = set()
        self._load_state()

    def _capacity_for(self, prefix_length):
        """Number of prefixes of the given length the pool can hold."""

        if self.capacity is not None:
            return self.capacity
        return 2 ** max(0, prefix_length - self._network.prefixlen)

    def request(
        self,
        duid,
        iaid,
        link=None,
        hop_count=0,
        prefix_length=None,
        rapid_commit=False,
        now=0.0,
        count=1,
    ):
        if hop_count > self.max_hops:
            raise Dhcpv6Error(
                "HopLimitExceeded",
                "relay hop count %d exceeds limit %d" % (hop_count, self.max_hops),
            )

        effective_length = self.prefix_length if prefix_length is None else prefix_length
        if not (MIN_PREFIX_LENGTH <= effective_length <= MAX_PREFIX_LENGTH):
            raise Dhcpv6Error(
                "InvalidPrefixLength",
                "prefix length %d is outside %d-%d"
                % (effective_length, MIN_PREFIX_LENGTH, MAX_PREFIX_LENGTH),
            )
        if effective_length < self._network.prefixlen:
            raise Dhcpv6Error(
                "InvalidPrefixLength",
                "prefix length %d is shorter than pool /%d"
                % (effective_length, self._network.prefixlen),
            )

        if count < 1:
            raise Dhcpv6Error("InvalidPrefixLength", "requested prefix count must be positive")

        key = (normalize_duid(duid), int(iaid))
        with self._lock:
            existing = self._bindings.get(key)
            if existing is not None:
                if now < existing["valid_expiry"]:
                    existing["link"] = link
                    return self._binding_view(existing, now)
                self._reclaim(key)

            capacity = self._capacity_for(effective_length)
            if len(self._allocated) + count > capacity:
                self._sweep_expired(now)
            if len(self._allocated) + count > capacity:
                raise Dhcpv6Error(
                    "NoPrefixAvailable",
                    "pool has no %d free prefix(es) of length /%d"
                    % (count, effective_length),
                )

            prefixes = self._allocate_prefixes(effective_length, count)
            binding = {
                "duid": key[0],
                "iaid": key[1],
                "link": link,
                "prefix_length": effective_length,
                "prefixes": prefixes,
                "preferred_expiry": now + self.preferred_lifetime,
                "valid_expiry": now + self.valid_lifetime,
            }
            self._bindings[key] = binding
            self._allocated.update(prefixes)
            self._save_state()
            return self._binding_view(binding, now)

    def renew(self, duid, iaid, now=0.0):
        key = (normalize_duid(duid), int(iaid))
        with self._lock:
            binding = self._bindings.get(key)
            if binding is None or now >= binding["valid_expiry"]:
                if binding is not None:
                    self._reclaim(key)
                    self._save_state()
                raise Dhcpv6Error("NoBinding", "no active lease for DUID/IAID pair")
            return self._binding_view(binding, now)

    def release(self, duid, iaid, now=0.0):
        key = (normalize_duid(duid), int(iaid))
        with self._lock:
            if key not in self._bindings:
                raise Dhcpv6Error("NoBinding", "no lease to release for DUID/IAID pair")
            self._reclaim(key)
            self._save_state()

    def usage(self, now=0.0):
        with self._lock:
            return sum(
                len(binding["prefixes"])
                for binding in self._bindings.values()
                if now < binding["valid_expiry"]
            )

    def _allocate_prefixes(self, prefix_length, count):
        prefixes = []
        for subnet in self._network.subnets(new_prefix=prefix_length):
            text = str(subnet)
            if text in self._allocated:
                continue
            prefixes.append(text)
            if len(prefixes) == count:
                return prefixes
        raise Dhcpv6Error(
            "NoPrefixAvailable",
            "pool exhausted before allocating %d prefix(es)" % count,
        )

    def _sweep_expired(self, now):
        """Reclaim leases only after their valid lifetime has elapsed."""

        expired = [
            key
            for key, binding in self._bindings.items()
            if now >= binding["valid_expiry"]
        ]
        for key in expired:
            self._reclaim(key)
        if expired:
            self._save_state()

    def _reclaim(self, key):
        binding = self._bindings.pop(key)
        self._allocated.difference_update(binding["prefixes"])

    def _binding_view(self, binding, now):
        valid_remaining = max(0, int(binding["valid_expiry"] - now))
        preferred_remaining = max(0, int(binding["preferred_expiry"] - now))
        return {
            "prefixes": list(binding["prefixes"]),
            "valid_lifetime": valid_remaining,
            "preferred_lifetime": preferred_remaining,
        }

    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        with open(self.state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        for binding in state.get("bindings", []):
            key = (binding["duid"], int(binding["iaid"]))
            self._bindings[key] = binding
            self._allocated.update(binding["prefixes"])

    def _save_state(self):
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = "%s.tmp" % self.state_path
        state = {"pool": str(self._network), "bindings": list(self._bindings.values())}
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.state_path)
