class VoiceError(ValueError):
    """Raised when a voice activity request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VoiceActivityGate:
    """Voice detection, hangover gating, ducking and device recovery."""

    _DUCKED_BUSES = ("game", "music")

    def __init__(self, hangover_ms=300, threshold_db=-40, duck_db=12):
        self.hangover_ms = hangover_ms
        self.threshold_db = threshold_db
        self.duck_db = duck_db
        self._users = {}

    def _user(self, name):
        return self._users.setdefault(name, {
            "state": "silent",
            "muted": False,
            "device": None,
            "last_voice_ms": None,
            "ptt": False,
            "gain": 1.0,
            "filter": "reset",
        })

    def detect(self, user, level_db, now_ms, push_to_talk=False,
               sample_rate=48000):
        if sample_rate <= 0:
            raise VoiceError("invalid_sample_rate", sample_rate)
        entry = self._user(user)
        if entry["muted"]:
            entry["state"] = "muted"
            return {"state": "muted", "onset": False}

        was_speaking = entry["state"] == "speaking"
        if push_to_talk:
            speaking = True
        elif level_db >= self.threshold_db:
            speaking = True
        else:
            speaking = False
            if entry["ptt"]:
                # Push-to-talk release closes immediately, no hangover.
                entry["last_voice_ms"] = None

        entry["ptt"] = bool(push_to_talk)
        if speaking:
            entry["state"] = "speaking"
            entry["last_voice_ms"] = now_ms
            return {"state": "speaking", "onset": not was_speaking}
        entry["state"] = "silent"
        return {"state": "silent", "onset": False}

    def gate(self, user, now_ms):
        entry = self._user(user)
        open_ = False
        if not entry["muted"] and entry["last_voice_ms"] is not None:
            open_ = now_ms - entry["last_voice_ms"] <= self.hangover_ms
        gain = 1.0 if open_ else 0.0
        entry["gain"] = gain
        return {"open": open_, "gain": gain}

    def duck(self, buses, active):
        out = {}
        for name, level in buses.items():
            if name in self._DUCKED_BUSES:
                if active:
                    out[name] = level - self.duck_db
                else:
                    out[name] = min(0.0, level + self.duck_db)
            else:
                out[name] = level
        return out

    def set_mute(self, user, muted):
        entry = self._user(user)
        entry["muted"] = bool(muted)
        if muted:
            entry["state"] = "muted"
            entry["last_voice_ms"] = None
            entry["ptt"] = False
        elif entry["state"] == "muted":
            entry["state"] = "silent"

    def set_device(self, user, device):
        self._user(user)["device"] = device

    def hotplug(self, user, device):
        entry = self._user(user)
        entry["device"] = device
        entry["gain"] = 1.0
        entry["filter"] = "reset"
        entry["last_voice_ms"] = None
        entry["ptt"] = False
        if not entry["muted"]:
            entry["state"] = "silent"
        return {"device": device, "gain": 1.0, "filter": "reset"}

    def recover(self, user=None):
        names = [user] if user is not None else list(self._users)
        recovered = []
        for name in names:
            entry = self._users.get(name)
            if entry is None:
                continue
            entry["last_voice_ms"] = None
            entry["ptt"] = False
            entry["gain"] = 1.0
            entry["filter"] = "reset"
            entry["state"] = "muted" if entry["muted"] else "silent"
            recovered.append(name)
        return recovered

    def state(self, user):
        entry = self._users.get(user)
        if entry is None:
            return {"state": "silent", "muted": False, "device": None,
                    "gain": 1.0, "filter": "reset"}
        return {
            "state": entry["state"],
            "muted": entry["muted"],
            "device": entry["device"],
            "gain": entry["gain"],
            "filter": entry["filter"],
        }

    def active_users(self):
        return [name for name, entry in self._users.items()
                if entry["state"] == "speaking" and not entry["muted"]]

    def snapshot(self):
        return {
            "settings": {
                "hangover_ms": self.hangover_ms,
                "threshold_db": self.threshold_db,
                "duck_db": self.duck_db,
            },
            "users": {
                name: {"muted": entry["muted"], "device": entry["device"]}
                for name, entry in self._users.items()
            },
        }

    def load(self, state):
        settings = state.get("settings", {})
        self.hangover_ms = settings.get("hangover_ms", self.hangover_ms)
        self.threshold_db = settings.get("threshold_db", self.threshold_db)
        self.duck_db = settings.get("duck_db", self.duck_db)
        self._users = {}
        for name, saved in state.get("users", {}).items():
            entry = self._user(name)
            entry["muted"] = bool(saved.get("muted", False))
            entry["device"] = saved.get("device")
            entry["state"] = "muted" if entry["muted"] else "silent"
