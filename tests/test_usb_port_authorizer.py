import pytest

import usb_port_authorizer


def _rule(rule_id, vid=0x1234, pid=0x5678, serial=None, device_class=None):
    rule = {"id": rule_id, "vid": vid, "pid": pid}
    if serial is not None:
        rule["serial"] = serial
    if device_class is not None:
        rule["class"] = device_class
    return rule


def _device(vid=0x1234, pid=0x5678, serial="SER-1", interfaces=()):
    return {
        "vid": vid,
        "pid": pid,
        "serial": serial,
        "interfaces": list(interfaces),
    }


def test_same_model_is_allowed_on_any_port():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    assert authorizer.authorize("port-1", _device())["authorized"] is True
    assert authorizer.authorize("port-2", _device())["authorized"] is True


def test_serial_only_rule_is_invalid():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    with pytest.raises(usb_port_authorizer.UsbError) as exc:
        authorizer.load_rules([{"id": "spoof", "serial": "SER-1"}])
    assert exc.value.code == "USB_RULE_INVALID"


def test_spoofed_serial_with_wrong_ids_is_denied():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage", serial="SER-1")])
    spoofed = _device(vid=0x9999, pid=0x9999, serial="SER-1")
    result = authorizer.authorize("port-1", spoofed)
    assert result["authorized"] is False
    assert result["driver_bound"] is False


def test_one_denied_interface_denies_composite_device():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([
        _rule("storage", vid=0x1234, pid=0x5678, device_class="storage"),
        _rule("net", vid=0x1111, pid=0x2222, device_class="net"),
    ])
    composite = _device(interfaces=[
        {"class": "storage", "vid": 0x1234, "pid": 0x5678},
        {"class": "net", "vid": 0x9999, "pid": 0x9999},
    ])
    result = authorizer.authorize("port-1", composite)
    assert result["authorized"] is False
    assert result["driver_bound"] is False


def test_unmatched_device_is_denied_before_driver_bind():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    result = authorizer.authorize("port-1", _device(vid=0x9999, pid=0x9999))
    assert result == {"authorized": False, "driver_bound": False,
                      "rule_id": None}


def test_denied_device_stays_present_without_driver():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    authorizer.authorize("port-1", _device(vid=0x9999, pid=0x9999))
    assert authorizer.device_present("port-1") is True
    assert authorizer.is_authorized("port-1") is False


def test_authorize_before_descriptors_is_rejected():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    with pytest.raises(usb_port_authorizer.UsbError) as exc:
        authorizer.authorize("port-1", None)
    assert exc.value.code == "USB_DESCRIPTOR_REQUIRED"


def test_released_port_cannot_be_authorized():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    authorizer.release_port("port-1")
    with pytest.raises(usb_port_authorizer.UsbError) as exc:
        authorizer.authorize("port-1", _device())
    assert exc.value.code == "USB_PORT_RELEASED"


def test_duplicate_authorization_is_idempotent():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    first = authorizer.authorize("port-1", _device())
    second = authorizer.authorize("port-1", _device())
    assert first == second
    assert second["rule_id"] == "storage"


def test_reload_failure_keeps_previous_policy():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    with pytest.raises(usb_port_authorizer.UsbError):
        authorizer.load_rules([{"id": "bad", "serial": "SER-1"}])
    assert authorizer.authorize("port-1", _device())["authorized"] is True


def test_successful_reload_applies_new_rules():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage", vid=0x1234, pid=0x5678)])
    authorizer.load_rules([_rule("net", vid=0x1111, pid=0x2222)])
    assert authorizer.authorize("port-1", _device())["authorized"] is False
    net_device = _device(vid=0x1111, pid=0x2222)
    assert authorizer.authorize("port-2", net_device)["authorized"] is True


def test_resume_requires_reauthorization():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    authorizer.authorize("port-1", _device())
    authorizer.suspend()
    resumed = authorizer.resume()
    assert resumed["reauthorize_ports"] == ["port-1"]
    assert authorizer.is_authorized("port-1") is False
    assert authorizer.authorize("port-1", _device())["authorized"] is True


def test_audit_log_contains_port_path():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    authorizer.authorize("port-7", _device())
    entry = authorizer.audit_log()[-1]
    assert entry["port_path"] == "port-7"
    assert entry["authorized"] is True


def test_out_of_order_events_are_bound_to_their_port():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([
        _rule("storage", vid=0x1234, pid=0x5678),
        _rule("net", vid=0x1111, pid=0x2222),
    ])
    authorizer.authorize("port-2", _device(vid=0x1111, pid=0x2222))
    authorizer.authorize("port-1", _device(vid=0x1234, pid=0x5678))
    entries = {entry["port_path"]: entry for entry in authorizer.audit_log()}
    assert entries["port-1"]["vid"] == 0x1234
    assert entries["port-2"]["vid"] == 0x1111


def test_failed_rule_load_never_degrades_to_allow_all():
    authorizer = usb_port_authorizer.UsbPortAuthorizer()
    authorizer.load_rules([_rule("storage")])
    with pytest.raises(usb_port_authorizer.UsbError):
        authorizer.load_rules([{"id": "bad"}])
    unknown = _device(vid=0x7777, pid=0x8888)
    assert authorizer.authorize("port-3", unknown)["authorized"] is False
