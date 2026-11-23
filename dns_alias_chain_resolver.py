"""DNS alias-chain resolver with loop and trust guard (skeleton)."""

import threading


class DnsError(Exception):
    """Configuration failure raised by the resolver."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class _Inflight(threading.Event):
    result = None


def _normalize_name(name):
    return name.rstrip(".").lower()


def _wire_name_length(name):
    labels = [label for label in name.split(".") if label]
    return sum(len(label) + 1 for label in labels) + 1


def _name_is_valid(name):
    labels = [label for label in name.split(".") if label]
    if any(len(label) > 63 for label in labels):
        return False
    return _wire_name_length(name) <= 255


class DnsAliasChainResolver:
    """Resolves CNAME chains without escaping trust boundaries."""

    def __init__(self, allowed_zones=("example.com",), max_depth=16, udp_size=512):
        self.allowed_zones = tuple(_normalize_name(zone) for zone in allowed_zones)
        self.max_depth = max_depth
        self.udp_size = udp_size
        self._records = {}
        self._cache = {}
        self._inflight = {}
        self._upstream_lookups = 0
        self._lock = threading.RLock()

    def add_record(self, name, rtype, value, ttl=60, rclass="IN", replace=False):
        normalized_name = _normalize_name(name)
        normalized_type = rtype.upper()
        normalized_value = _normalize_name(value) if normalized_type == "CNAME" else value
        record = {
            "rtype": normalized_type,
            "value": normalized_value,
            "ttl": int(ttl),
            "rclass": rclass.upper(),
        }
        with self._lock:
            records = self._records.setdefault(normalized_name, [])
            if replace:
                records[:] = [item for item in records if item["rtype"] != normalized_type]
            records.append(record)
            stale_keys = [cache_key for cache_key in self._cache if cache_key[0] == normalized_name]
            for cache_key in stale_keys:
                del self._cache[cache_key]

    def resolve(self, name, rtype="A", now=0.0):
        qname = _normalize_name(name)
        qtype = rtype.upper()
        cached = self._cache_entry(qname, qtype, now)
        if cached is not None:
            return cached

        key = (qname, qtype)
        with self._lock:
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = _Inflight()
                self._inflight[key] = flight

        if not leader:
            flight.wait()
            return flight.result

        try:
            try:
                result = self._cache_entry(qname, qtype, now)
                if result is None:
                    result = self._resolve_uncached(qname, qtype, now)
            except Exception:
                result = {"status": "SERVFAIL", "error": "INTERNAL", "answers": []}
            flight.result = result
            flight.set()
        finally:
            with self._lock:
                self._inflight.pop(key, None)
        return result

    def cache_lookup(self, name, now=0.0):
        normalized_name = _normalize_name(name)
        with self._lock:
            for key in list(self._cache):
                if key[0] != normalized_name:
                    continue
                entry = self._cache[key]
                if entry["expires"] <= now:
                    del self._cache[key]
                    continue
                return entry
        return None

    def upstream_count(self):
        with self._lock:
            return self._upstream_lookups

    def _cache_entry(self, name, rtype, now):
        with self._lock:
            for key in ((name, rtype), (name, "*")):
                entry = self._cache.get(key)
                if entry is None:
                    continue
                if entry["expires"] <= now:
                    del self._cache[key]
                    continue
                return entry
        return None

    def _store_cache(self, name, rtype, entry, now):
        entry = dict(entry)
        entry["expires"] = now + entry["ttl"]
        with self._lock:
            self._cache[(name, rtype)] = entry
        return entry

    def _in_allowed_zone(self, name):
        return any(
            name == zone or name.endswith("." + zone)
            for zone in self.allowed_zones
        )

    def _servfail(self, error):
        return {"status": "SERVFAIL", "error": error, "answers": []}

    def _resolve_uncached(self, qname, qtype, now):
        with self._lock:
            self._upstream_lookups += 1

        query_class = "IN"
        negative_ttl = 30
        visited = set()
        answers = []
        min_ttl = None
        current = qname
        depth = 0

        while True:
            if not _name_is_valid(current):
                return self._servfail("NAME_TOO_LONG")
            if current in visited:
                return self._servfail("CNAME_LOOP")
            if depth > self.max_depth:
                return self._servfail("CNAME_CHAIN_TOO_LONG")
            visited.add(current)

            cached = self._cache_entry(current, qtype, now)
            if cached is not None:
                if cached["status"] == "NXDOMAIN":
                    return {"status": "NXDOMAIN", "answers": []}
                return self._finalize_success(qname, qtype, cached["answers"], cached["ttl"], now)

            records = self._records.get(current, [])
            if not records:
                negative = {"status": "NXDOMAIN", "answers": [], "ttl": negative_ttl}
                self._store_cache(current, "*", negative, now)
                return {"status": "NXDOMAIN", "answers": []}

            if any(record["rclass"] != query_class for record in records):
                return self._servfail("CLASS_MISMATCH")

            cname_records = [record for record in records if record["rtype"] == "CNAME"]
            other_records = [record for record in records if record["rtype"] != "CNAME"]
            if cname_records and other_records:
                return self._servfail("CNAME_CONFLICT")

            if cname_records:
                cname = cname_records[0]
                target = cname["value"]
                if not _name_is_valid(target):
                    return self._servfail("NAME_TOO_LONG")
                if not self._in_allowed_zone(target):
                    return self._servfail("OUT_OF_ZONE")
                answers.append((current, "CNAME", target))
                min_ttl = cname["ttl"] if min_ttl is None else min(min_ttl, cname["ttl"])
                current = target
                depth += 1
                continue

            matching = [record for record in records if record["rtype"] == qtype]
            for record in matching:
                answers.append((current, record["rtype"], record["value"]))
                min_ttl = record["ttl"] if min_ttl is None else min(min_ttl, record["ttl"])
            if min_ttl is None:
                min_ttl = min(record["ttl"] for record in records)
            return self._finalize_success(qname, qtype, answers, min_ttl, now)

    def _finalize_success(self, qname, qtype, answers, min_ttl, now):
        truncated = self._estimate_wire_size(qname, answers) > self.udp_size
        entry = {
            "status": "NOERROR",
            "answers": list(answers),
            "ttl": min_ttl,
            "truncated": truncated,
        }
        return self._store_cache(qname, qtype, entry, now)

    def _estimate_wire_size(self, qname, answers):
        size = 12 + _wire_name_length(qname) + 4
        for owner, rtype, value in answers:
            rdata_length = 4 if rtype == "A" else _wire_name_length(value)
            size += _wire_name_length(owner) + 10 + rdata_length
        return size
