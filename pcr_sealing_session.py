"""TPM PCR sealing and policy-session lifecycle (skeleton)."""


class TpmError(Exception):
    """Unseal or policy failure raised by the sealer."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class PcrSealingSession:
    """Seals disk keys to PCR values and enforces single-use policy sessions."""

    def __init__(self, expected_pcrs=(1, 2, 3), max_slots=2, state_path=None):
        self.expected_pcrs = tuple(expected_pcrs)
        self.max_slots = max_slots
        self.state_path = state_path

    def start_policy(self, nonce=None):
        raise NotImplementedError

    def unseal(
        self,
        policy_handle,
        pcr_values,
        key_id="disk-1",
        hash_alg="sha256",
        pcr_extended=False,
        now=0.0,
        signature_expired=False,
    ):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def module_reset(self):
        raise NotImplementedError

    def boot_policy_change(self):
        raise NotImplementedError

    def sealed_key(self, key_id):
        raise NotImplementedError

    def policy_count(self):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
