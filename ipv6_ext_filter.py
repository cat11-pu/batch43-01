"""IPv6 extension-header and fragment filter."""


class Ipv6FilterError(Exception):
    """Configuration failure raised by the filter."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class Ipv6ExtFilter:
    """Validates IPv6 extension chains, fragments and ICMPv6 errors."""

    HOP_BY_HOP = 0
    ROUTING = 43
    FRAGMENT = 44
    DESTINATION = 60
    JUMBO_OPTION = 0xC2
    MAX_FLOW_LABEL = 0xFFFFF

    def __init__(self, max_ext_headers=8):
        self.max_ext_headers = max_ext_headers
        self.counters = {}
        self._flows = set()
        self._frag_queues = {}

    def register_flow(self, src, dst, sport, dport, proto="tcp"):
        self._flows.add((src, dst, sport, dport, proto))

    def _drop(self, code, **extra):
        self.counters[code] = self.counters.get(code, 0) + 1
        result = {"action": "drop", "code": code}
        result.update(extra)
        return result

    def filter_packet(
        self,
        headers=(),
        src="2001:db8::1",
        dst="2001:db8::2",
        fragment=None,
        flow_label=0,
        icmp_error=None,
        payload_len=0,
    ):
        headers = list(headers)

        if flow_label < 0 or flow_label > self.MAX_FLOW_LABEL:
            return self._drop("IPV6_FLOW_LABEL_INVALID")

        if len(headers) > self.max_ext_headers:
            return self._drop("IPV6_EXT_CHAIN_TOO_LONG")

        fragment_seen = False
        for index, header in enumerate(headers):
            htype = header.get("type")

            if htype == self.HOP_BY_HOP and index != 0:
                return self._drop("IPV6_EXT_ORDER")

            if htype == self.FRAGMENT:
                if fragment_seen:
                    return self._drop("IPV6_DOUBLE_FRAG")
                fragment_seen = True

            if htype == self.ROUTING and header.get("routing_type") == 0:
                return self._drop("IPV6_RH0_DENIED", icmp={"type": 4, "code": 0})

            for option in header.get("options", []) or []:
                if (
                    htype == self.HOP_BY_HOP
                    and option.get("type") == self.JUMBO_OPTION
                    and option.get("value", 0) > payload_len
                ):
                    return self._drop("IPV6_JUMBO_INVALID")
                if htype == self.DESTINATION:
                    action = option.get("action")
                    if action is None:
                        action = (option.get("type", 0) >> 6) & 0x3
                    if action != 0:
                        return self._drop("IPV6_UNKNOWN_OPTION")

        if icmp_error is not None:
            key = (
                icmp_error.get("src"),
                icmp_error.get("dst"),
                icmp_error.get("sport"),
                icmp_error.get("dport"),
                icmp_error.get("proto"),
            )
            if key not in self._flows:
                return self._drop("ICMPV6_ORPHAN")

        return {"action": "forward", "code": None}

    def reassemble(self, src, ident, offset, payload, more=True):
        key = (src, ident)
        queue = self._frag_queues.setdefault(key, [])

        start = offset
        end = offset + len(payload)
        for existing_offset, existing_payload, _ in queue:
            existing_end = existing_offset + len(existing_payload)
            if start < existing_end and existing_offset < end:
                self.counters["IPV6_FRAG_OVERLAP"] = (
                    self.counters.get("IPV6_FRAG_OVERLAP", 0) + 1
                )
                return {"complete": False, "code": "IPV6_FRAG_OVERLAP"}

        queue.append((offset, payload, more))

        total = None
        for frag_offset, frag_payload, frag_more in queue:
            if not frag_more:
                total = frag_offset + len(frag_payload)
        if total is None:
            return {"complete": False, "code": None}

        assembled = bytearray(total)
        covered = 0
        for frag_offset, frag_payload, _ in sorted(queue, key=lambda f: f[0]):
            if frag_offset > covered:
                return {"complete": False, "code": None}
            assembled[frag_offset : frag_offset + len(frag_payload)] = frag_payload
            covered = max(covered, frag_offset + len(frag_payload))
        if covered < total:
            return {"complete": False, "code": None}

        del self._frag_queues[key]
        return {"complete": True, "code": None, "data": bytes(assembled)}
