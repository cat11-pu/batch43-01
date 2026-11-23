"""MQTT topic authorization, session and delivery matrix.

The matrix enforces the park security policy in one place:

* subscriptions are matched level by level and may never leave the tenant
  prefix, and ``$SYS`` system topics are blocked for normal devices;
* ACL entries are consulted on every operation, so changes take effect
  without restarting the broker;
* QoS2 retransmissions are de-duplicated by packet id;
* a ``client_id`` owns a single active session and a new connection takes
  over by closing the old one;
* oversized or malformed payloads disconnect the client and are audited
  instead of being truncated and forwarded.
"""


class MqttError(Exception):
    """Policy failure raised by the matrix."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def _split_topic(topic):
    return topic.split("/") if topic else [""]


def _topic_matches(topic, topic_filter):
    """Return True when a concrete topic matches an MQTT filter.

    Matching walks the levels one by one: ``+`` matches a single level and
    ``#`` matches the current level and every level below it.
    """
    topic_levels = _split_topic(topic)
    filter_levels = _split_topic(topic_filter)
    index = 0
    while index < len(filter_levels):
        level = filter_levels[index]
        if level == "#":
            return True
        if index >= len(topic_levels):
            return False
        if level != "+" and level != topic_levels[index]:
            return False
        index += 1
    return index == len(topic_levels)


class _Session:
    """Connection-scoped state for one client id."""

    def __init__(self, client_id, username, token):
        self.client_id = client_id
        self.username = username
        self.token = token
        self.active = True
        self.subscriptions = []
        self.shared_groups = {}
        self.inbox = []
        self.retained_inbox = []
        self.inflight = {}
        self.queued = []
        self.delivered_qos2 = set()
        self.will = None


class MqttTopicMatrix:
    """Authorizes topic subscriptions and owns MQTT session delivery state."""

    def __init__(self, tenant_prefix="tenant-a", inflight_window=4, max_payload=256):
        self.tenant_prefix = tenant_prefix
        self.inflight_window = inflight_window
        self.max_payload = max_payload
        self.sessions = {}
        self.acls = {}
        self.retained = {}
        self.shared_groups = {}
        self.bridges = {}
        self._audit = []
        self._next_token = 0

    def _audit_event(self, code, **detail):
        event = {"code": code}
        event.update(detail)
        self._audit.append(event)
        return event

    def _require_session(self, client_id):
        session = self.sessions.get(client_id)
        if session is None or not session.active:
            raise MqttError("SESSION_INACTIVE", "client is not connected")
        return session

    def _filter_allowed(self, username, topic_filter):
        """Check an ACL filter against the username's allow/deny entries."""
        for acl_filter, allowed in self.acls.get(username, ()):
            if _topic_matches(topic_filter, acl_filter):
                return allowed
        return True

    def _authorize_subscription(self, username, topic_filter):
        if topic_filter.startswith("$"):
            self._audit_event(
                "SYS_TOPIC_DENIED",
                username=username,
                topic_filter=topic_filter,
            )
            raise MqttError("SYS_TOPIC_DENIED")
        if not self._within_tenant(topic_filter):
            self._audit_event(
                "SUB_DENIED",
                username=username,
                topic_filter=topic_filter,
                reason="outside tenant prefix",
            )
            raise MqttError("SUB_DENIED")
        if not self._filter_allowed(username, topic_filter):
            self._audit_event(
                "SUB_DENIED",
                username=username,
                topic_filter=topic_filter,
                reason="acl deny",
            )
            raise MqttError("SUB_DENIED")

    def _within_tenant(self, topic_filter):
        """Wildcards must not reach above or sideways of the tenant prefix."""
        levels = _split_topic(topic_filter)
        tenant_levels = _split_topic(self.tenant_prefix)
        if len(levels) < len(tenant_levels):
            return False
        for index, tenant_level in enumerate(tenant_levels):
            level = levels[index]
            if level == "#" or level == "+" or level != tenant_level:
                return False
        return True

    def _queue_retained(self, session, topic_filter):
        for topic, payload in self.retained.items():
            if not _topic_matches(topic, topic_filter):
                continue
            if not self._filter_allowed(session.username, topic):
                continue
            session.retained_inbox.append((topic, payload))

    def add_acl(self, username, topic_filter, allow=True):
        self.acls.setdefault(username, []).append((topic_filter, allow))

    def connect(self, client_id, username, tls_cn=None):
        if tls_cn is not None and tls_cn != username:
            self._audit_event(
                "TLS_CN_MISMATCH",
                client_id=client_id,
                username=username,
                tls_cn=tls_cn,
            )
            raise MqttError("TLS_CN_MISMATCH")
        previous = self.sessions.get(client_id)
        if previous is not None and previous.active:
            self._close_session(previous, graceful=False, deliver_will=False)
        self._next_token += 1
        token = self._next_token
        self.sessions[client_id] = _Session(client_id, username, token)
        return token

    def _close_session(self, session, graceful, deliver_will):
        session.active = False
        for group in list(session.shared_groups):
            members = self.shared_groups.get(group)
            if members and session.client_id in members:
                members.remove(session.client_id)
        will = session.will
        session.will = None
        if will is not None and not graceful and deliver_will:
            will_topic, will_payload = will
            self._deliver(will_topic, will_payload)

    def is_active(self, client_id, token):
        session = self.sessions.get(client_id)
        return session is not None and session.active and session.token == token

    def subscribe(self, client_id, topic_filter):
        session = self._require_session(client_id)
        self._authorize_subscription(session.username, topic_filter)
        if topic_filter not in session.subscriptions:
            session.subscriptions.append(topic_filter)
        self._queue_retained(session, topic_filter)
        return True

    def subscribe_shared(self, client_id, group, topic_filter):
        session = self._require_session(client_id)
        self._authorize_subscription(session.username, topic_filter)
        session.shared_groups[group] = topic_filter
        members = self.shared_groups.setdefault(group, [])
        if client_id not in members:
            members.append(client_id)
        self._queue_retained(session, topic_filter)
        return True

    def shared_members(self, group):
        return list(self.shared_groups.get(group, ()))

    def publish(self, client_id, topic, payload, qos=0, packet_id=None, retain=False):
        session = self._require_session(client_id)
        if not isinstance(payload, (bytes, bytearray)):
            self._audit_event(
                "MALFORMED_PAYLOAD",
                client_id=client_id,
                topic=topic,
                payload_type=type(payload).__name__,
            )
            self._close_session(session, graceful=False, deliver_will=False)
            raise MqttError("MALFORMED_PAYLOAD")
        payload = bytes(payload)
        if len(payload) > self.max_payload:
            self._audit_event(
                "PAYLOAD_TOO_LARGE",
                client_id=client_id,
                topic=topic,
                size=len(payload),
                limit=self.max_payload,
            )
            self._close_session(session, graceful=False, deliver_will=False)
            raise MqttError("PAYLOAD_TOO_LARGE")
        if qos == 2 and packet_id is not None:
            if packet_id in session.delivered_qos2:
                return 0
            session.delivered_qos2.add(packet_id)
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)
        if qos == 0:
            return self._deliver(topic, payload)
        if len(session.inflight) >= self.inflight_window:
            session.queued.append((topic, payload, qos, packet_id))
            return 0
        return self._send_inflight(session, topic, payload, qos, packet_id)

    def _send_inflight(self, session, topic, payload, qos, packet_id):
        if packet_id is not None:
            session.inflight[packet_id] = (topic, payload)
        return self._deliver(topic, payload)

    def _pump(self, session):
        while session.queued and len(session.inflight) < self.inflight_window:
            topic, payload, qos, packet_id = session.queued.pop(0)
            self._send_inflight(session, topic, payload, qos, packet_id)

    def _deliver(self, topic, payload):
        delivered = 0
        for session in self.sessions.values():
            if not session.active:
                continue
            if any(_topic_matches(topic, rule) for rule in session.subscriptions):
                session.inbox.append((topic, payload))
                delivered += 1
        for group, members in self.shared_groups.items():
            active_members = [
                client_id
                for client_id in members
                if client_id in self.sessions
                and self.sessions[client_id].active
                and group in self.sessions[client_id].shared_groups
            ]
            for client_id in active_members:
                member_session = self.sessions[client_id]
                rule = member_session.shared_groups[group]
                if _topic_matches(topic, rule):
                    chosen = active_members[0]
                    self.sessions[chosen].inbox.append((topic, payload))
                    delivered += 1
                    break
        return delivered

    def ack(self, client_id, packet_id):
        session = self._require_session(client_id)
        session.inflight.pop(packet_id, None)
        self._pump(session)

    def pending_count(self, client_id):
        session = self.sessions.get(client_id)
        if session is None or not session.active:
            return 0
        return len(session.queued)

    def take_messages(self, client_id):
        session = self._require_session(client_id)
        messages = session.inbox
        session.inbox = []
        return messages

    def take_retained(self, client_id):
        session = self._require_session(client_id)
        messages = session.retained_inbox
        session.retained_inbox = []
        return messages

    def set_will(self, client_id, topic, payload):
        session = self._require_session(client_id)
        session.will = (topic, payload)

    def disconnect(self, client_id, graceful=True):
        session = self.sessions.get(client_id)
        if session is None or not session.active:
            return
        self._close_session(session, graceful=graceful, deliver_will=True)

    def add_bridge(self, name, direction):
        if direction == "both":
            self._audit_event("BRIDGE_LOOP", bridge=name, direction=direction)
            raise MqttError("BRIDGE_LOOP")
        self.bridges[name] = direction

    def audit_events(self):
        return list(self._audit)
