"""fscrypt keyring and inode encryption supervisor (skeleton)."""


class FscryptError(Exception):
    """Key or policy failure raised by the supervisor."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class FscryptKeyringSupervisor:
    """Tracks encryption keys, open handles and per-inode key generations."""

    def __init__(self, fs_supports_encryption=True, policy_version=2):
        self.fs_supports_encryption = fs_supports_encryption
        self.policy_version = policy_version

    def add_key(self, descriptor, master_key, policy_version=None):
        raise NotImplementedError

    def open_file(self, inode_id, descriptor):
        raise NotImplementedError

    def close_file(self, inode_id):
        raise NotImplementedError

    def remove_key(self, descriptor):
        raise NotImplementedError

    def write_file(self, inode_id, descriptor, data):
        raise NotImplementedError

    def read_file(self, inode_id):
        raise NotImplementedError

    def rotate_key(self, descriptor, new_master_key):
        raise NotImplementedError

    def restart(self, session_reset=False):
        raise NotImplementedError

    def suspend(self):
        raise NotImplementedError

    def key_status(self, descriptor):
        raise NotImplementedError

    def inode_generation(self, inode_id):
        raise NotImplementedError

    def files(self):
        raise NotImplementedError
