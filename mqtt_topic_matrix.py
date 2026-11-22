"""MQTT topic authorization, session and delivery matrix (skeleton)."""


class MqttError(Exception):
    """Policy failure raised by the matrix."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class MqttTopicMatrix:
    """Authorizes topic subscriptions and owns MQTT session delivery state."""

    def __init__(self, tenant_prefix="tenant-a", inflight_window=4, max_payload=256):
        self.tenant_prefix = tenant_prefix
        self.inflight_window = inflight_window
        self.max_payload = max_payload

    def add_acl(self, username, topic_filter, allow=True):
        raise NotImplementedError

    def connect(self, client_id, username, tls_cn=None):
        raise NotImplementedError

    def is_active(self, client_id, token):
        raise NotImplementedError

    def subscribe(self, client_id, topic_filter):
        raise NotImplementedError

    def subscribe_shared(self, client_id, group, topic_filter):
        raise NotImplementedError

    def shared_members(self, group):
        raise NotImplementedError

    def publish(self, client_id, topic, payload, qos=0, packet_id=None, retain=False):
        raise NotImplementedError

    def ack(self, client_id, packet_id):
        raise NotImplementedError

    def pending_count(self, client_id):
        raise NotImplementedError

    def take_messages(self, client_id):
        raise NotImplementedError

    def take_retained(self, client_id):
        raise NotImplementedError

    def set_will(self, client_id, topic, payload):
        raise NotImplementedError

    def disconnect(self, client_id, graceful=True):
        raise NotImplementedError

    def add_bridge(self, name, direction):
        raise NotImplementedError

    def audit_events(self):
        raise NotImplementedError
