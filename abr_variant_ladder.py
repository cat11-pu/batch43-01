class AbrError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AbrVariantLadder:
    """ABR variant ladder with boundary-only atomic switches.

    Switching rules:
      * a switch is only applied at a segment boundary, never mid-segment;
      * the target segment must carry an aligned keyframe and share the
        current discontinuity sequence number;
      * audio and video switch together as one variant group on one shared
        timeline;
      * the target init segment is prefetched before the switch;
      * bandwidth is estimated over a sliding window instead of a single
        segment;
      * buffer level is tracked for audio and video separately and the
        smaller one drives the decision;
      * manifest reloads prune removed variants and force a re-selection;
      * live and vod get independent controller instances.

    Hard gates:
      * the audio/video timeline must stay continuous across every switch;
      * a failed switch falls back to the previous usable variant instead of
        leaving the player on a black screen.
    """

    WINDOW_SIZE = 8
    BUFFER_LOW_SECONDS = 1.0
    RECOVER_GOOD_REQUIRED = 3

    def __init__(self, mode="live"):
        self.mode = mode
        self.variants = {}
        self.ordered = []
        self.current_id = None
        self.segments = {}
        self.bandwidth_window = []
        self.last_seq = None
        self.discontinuity = 0
        self.pending = None
        self.stalls = 0
        self.in_recovery = False
        self.good_samples = 0
        self.prefetched_inits = set()

    def load_manifest(self, variants):
        self.variants = {item["id"]: dict(item) for item in variants}
        self.ordered = sorted(self.variants.values(),
                              key=lambda item: item["bitrate"])
        if self.pending is not None and self.pending["target"] not in self.variants:
            self.pending = None
        if not self.variants:
            self.current_id = None
        elif self.current_id not in self.variants:
            self.current_id = self.ordered[0]["id"]
        return self.current_id

    def add_segment(self, seq, info):
        self.segments[seq] = info
        return info

    def decide(self, video_buffer=10.0, audio_buffer=10.0,
               bandwidth_bps=None):
        if not self.variants:
            raise AbrError("EMPTY_MANIFEST")

        lowest = self.ordered[0]
        buffer_level = min(video_buffer, audio_buffer)
        if buffer_level < self.BUFFER_LOW_SECONDS:
            target = lowest
            reason = "BUFFER_UNDERRUN"
        else:
            estimate = bandwidth_bps
            if estimate is None:
                estimate = self._estimate_bandwidth()
            target = self._select_for_bandwidth(estimate)
            reason = "BANDWIDTH"

        current = self.variants[self.current_id]
        if target["bitrate"] > current["bitrate"] and self._upgrade_held():
            target = current
            reason = "RECOVERY_HOLD"

        return {
            "target": target["id"],
            "audio": target["audio"],
            "bitrate": target["bitrate"],
            "reason": reason,
            "estimate_bps": self._estimate_bandwidth(),
            "buffer_seconds": buffer_level,
        }

    def request_switch(self, target, seq):
        if target not in self.variants:
            raise AbrError("UNKNOWN_VARIANT", target)

        init_required = self._init_required(target)
        result = {
            "target": target,
            "switched": False,
            "deferred": False,
            "reason": None,
            "init_required": init_required,
            "init": self.variants[target]["init"] if init_required else None,
        }

        if target == self.current_id:
            result["reason"] = "SAME_VARIANT"
            return result

        next_seq = (seq if seq is not None else self.last_seq or 0) + 1
        info = self.segments.get(next_seq)
        if info is not None:
            blocked = self._block_reason(info, target)
            if blocked is not None:
                result["reason"] = blocked
                return result

        self.pending = {"target": target, "boundary_seq": next_seq}
        result["deferred"] = True
        result["reason"] = "DEFERRED"
        return result

    def on_segment_end(self, seq, bandwidth_bps=None, stall=False):
        self._check_continuity(seq)
        self.last_seq = seq

        if stall:
            self.stalls = 0
            self.good_samples = 0
            self.in_recovery = True
            self.pending = None
            self.current_id = self.ordered[0]["id"]

        switched = False
        reason = None
        if self.pending is not None and not stall:
            switched, reason = self._apply_pending_switch(seq)

        if bandwidth_bps is not None:
            self.bandwidth_window.append(bandwidth_bps)
            if len(self.bandwidth_window) > self.WINDOW_SIZE:
                self.bandwidth_window.pop(0)
            if self.current_id is not None and \
                    bandwidth_bps >= self.variants[self.current_id]["bitrate"]:
                self.good_samples += 1
            else:
                self.good_samples = 0

        return {
            "seq": seq,
            "current": self.current_id,
            "audio": self._audio_of(self.current_id),
            "switched": switched,
            "reason": reason,
        }

    def current(self):
        return self.current_id

    def state(self):
        return {
            "mode": self.mode,
            "current": self.current_id,
            "audio": self._audio_of(self.current_id),
            "last_seq": self.last_seq or 0,
            "discontinuity": self.discontinuity,
            "stalls": self.stalls,
            "in_recovery": self.in_recovery,
            "good_samples": self.good_samples,
            "pending": None if self.pending is None else dict(self.pending),
            "estimate_bps": self._estimate_bandwidth(),
            "prefetched_inits": sorted(self.prefetched_inits),
        }

    def seek(self, seq):
        self.last_seq = seq - 1
        self.pending = None
        self.bandwidth_window = []
        self.stalls = 0
        self.good_samples = 0
        self.in_recovery = False
        return {"seq": seq, "current": self.current_id,
                "audio": self._audio_of(self.current_id)}

    def init_segment(self, variant_id):
        if variant_id not in self.variants:
            raise AbrError("UNKNOWN_VARIANT", variant_id)
        init = self.variants[variant_id]["init"]
        self.prefetched_inits.add(init)
        return init

    def dump(self):
        return {
            "mode": self.mode,
            "variants": [dict(item) for item in self.ordered],
            "current_id": self.current_id,
            "segments": {seq: dict(info)
                         for seq, info in self.segments.items()},
            "bandwidth_window": list(self.bandwidth_window),
            "last_seq": self.last_seq,
            "discontinuity": self.discontinuity,
            "pending": None if self.pending is None else dict(self.pending),
            "stalls": self.stalls,
            "in_recovery": self.in_recovery,
            "good_samples": self.good_samples,
            "prefetched_inits": sorted(self.prefetched_inits),
        }

    def load(self, state):
        self.mode = state.get("mode", self.mode)
        self.load_manifest(state.get("variants", []))
        self.segments = {seq: dict(info)
                         for seq, info in state.get("segments", {}).items()}
        self.bandwidth_window = list(state.get("bandwidth_window", []))
        self.last_seq = state.get("last_seq")
        self.discontinuity = state.get("discontinuity", 0)
        self.pending = (None if state.get("pending") is None
                        else dict(state["pending"]))
        self.stalls = state.get("stalls", 0)
        self.in_recovery = state.get("in_recovery", False)
        self.good_samples = state.get("good_samples", 0)
        self.prefetched_inits = set(state.get("prefetched_inits", []))
        return self.state()

    def _estimate_bandwidth(self):
        if not self.bandwidth_window:
            return None
        return sum(self.bandwidth_window) / len(self.bandwidth_window)

    def _select_for_bandwidth(self, bandwidth_bps):
        if bandwidth_bps is None:
            return self.ordered[0]
        chosen = self.ordered[0]
        for variant in self.ordered:
            if variant["bitrate"] <= bandwidth_bps:
                chosen = variant
            else:
                break
        return chosen

    def _upgrade_held(self):
        return self.in_recovery and \
            self.good_samples < self.RECOVER_GOOD_REQUIRED

    def _init_required(self, target):
        if target == self.current_id:
            return False
        current = self.variants.get(self.current_id)
        if current is None:
            return True
        return current["codec"] != self.variants[target]["codec"]

    def _block_reason(self, segment_info, target):
        if segment_info.get("discontinuity", 0) != self.discontinuity:
            return "DISCONTINUITY"
        variant_info = segment_info.get("variants", {}).get(target)
        if not variant_info or not variant_info.get("keyframe", False):
            return "NO_KEYFRAME"
        return None

    def _check_continuity(self, seq):
        if self.last_seq is None:
            return
        info = self.segments.get(seq)
        declared = info.get("discontinuity", self.discontinuity) \
            if info is not None else self.discontinuity
        if declared != self.discontinuity:
            self.discontinuity = declared
            return
        if seq != self.last_seq + 1:
            raise AbrError(
                "CONTINUITY_ERROR",
                {"seq": seq, "expected": self.last_seq + 1},
            )

    def _apply_pending_switch(self, seq):
        pending = self.pending
        target = pending["target"]
        info = self.segments.get(seq)
        if info is None:
            self.pending = None
            return False, "NO_SEGMENT_INFO"
        blocked = self._block_reason(info, target)
        if blocked is not None:
            self.pending = None
            return False, blocked

        self.init_segment(target)
        self.current_id = target
        self.pending = None
        self.stalls = 0
        self.in_recovery = False
        self.good_samples = 0
        return True, "SWITCHED"

    def _audio_of(self, variant_id):
        if variant_id is None or variant_id not in self.variants:
            return None
        return self.variants[variant_id]["audio"]
