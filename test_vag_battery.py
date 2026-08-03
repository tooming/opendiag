#!/usr/bin/env python3
"""Tests for VAG battery-replacement coding. Verifies the software layer
AND the safety gate that blocks a real-car write until the DIDs are
confirmed (see vag_battery.py's module docstring)."""
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vag_battery
from transaction import get_transaction_manager


def test_unknown_technology_rejected():
    try:
        vag_battery.build_write_plan(70, "gel")
        assert False, "should have raised"
    except ValueError:
        pass
    print("test_unknown_technology_rejected OK")


def test_gate_holds_when_dids_unconfirmed():
    """No CHANNEL_SPECS DID is set -> plan is refused, not guessed."""
    plan, note = vag_battery.build_write_plan(70, "agm", manufacturer="VARTA",
                                               serial="1234567890")
    assert plan is None, plan
    assert "no confirmed UDS DID" in note, note
    assert "capacity_ah" in note and "technology" in note, note
    assert "manufacturer" in note and "serial" in note, note
    print("test_gate_holds_when_dids_unconfirmed OK:", note)


def test_plan_builds_once_dids_confirmed():
    """Simulating a confirmed trace: fill in DIDs, plan becomes concrete,
    then restore so nothing leaks into other tests."""
    saved = {f: spec["did"] for f, spec in vag_battery.CHANNEL_SPECS.items()}
    try:
        for i, f in enumerate(vag_battery.CHANNEL_SPECS):
            vag_battery.CHANNEL_SPECS[f]["did"] = 0xF100 + i  # hypothetical
        plan, note = vag_battery.build_write_plan(70, "agm",
                                                   manufacturer="VARTA",
                                                   serial="1234567890")
        assert plan is not None, note
        fields = {p["field"] for p in plan}
        assert fields == {"capacity_ah", "technology", "manufacturer", "serial"}
        cap = next(p for p in plan if p["field"] == "capacity_ah")
        assert cap["data"] == bytes([70])
        tech = next(p for p in plan if p["field"] == "technology")
        assert tech["data"] == bytes([1])  # agm == 1
    finally:
        for f, did in saved.items():
            vag_battery.CHANNEL_SPECS[f]["did"] = did
    assert all(spec["did"] is None for spec in vag_battery.CHANNEL_SPECS.values())
    print("test_plan_builds_once_dids_confirmed OK")


def test_real_car_refuses_without_confirmed_dids():
    """A non-demo adapter (has a `.uds`) must be hard-refused before any
    write is attempted -- the transaction layer is never even entered."""
    fake_uds = object()  # truthy stand-in; code_battery must never touch it
    adapter = types.SimpleNamespace(name="Octavia Gateway", vin="TMBJJ0000K0000000",
                                     uds=fake_uds)
    result = vag_battery.code_battery(adapter, 70, "agm")
    assert result["gated"] is True
    assert result["success"] is False
    assert "unconfirmed" in result["error"] or "no confirmed" in result["error"]
    print("test_real_car_refuses_without_confirmed_dids OK")


def test_demo_flow_runs_through_transaction():
    """Demo mode exercises the whole read->backup->write->verify pipeline
    without needing any confirmed DID."""
    tmp = tempfile.mkdtemp()
    tm = get_transaction_manager(backup_root=tmp)

    adapter = types.SimpleNamespace(name="DEMO — simulated Octavia Gateway",
                                     vin="DEMOVIN", uds=None)
    r = vag_battery.code_battery(adapter, 70, "agm", manufacturer="VARTA",
                                  serial="1234567890", transaction_manager=tm,
                                  user_note="unit test")
    assert r["demo"] is True
    assert r["success"] is True, r
    assert r["verified"] is True, r
    backups = tm.list_backups("DEMOVIN")
    assert len(backups) >= 1, backups
    shutil.rmtree(tmp)
    print("test_demo_flow_runs_through_transaction OK")


if __name__ == "__main__":
    test_unknown_technology_rejected()
    test_gate_holds_when_dids_unconfirmed()
    test_plan_builds_once_dids_confirmed()
    test_real_car_refuses_without_confirmed_dids()
    test_demo_flow_runs_through_transaction()
    print("\nAll vag_battery tests passed.")
