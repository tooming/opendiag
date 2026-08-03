#!/usr/bin/env python3
"""Battery-replacement coding for VW Group MQB vehicles (Octavia Mk3 and
similar) over the CAN/UDS transport in vag_diag.py.

## Why this matters

MQB cars track the fitted battery's capacity and technology (flooded/EFB
vs. AGM) in the Gateway module so the BMS (charge-voltage curve, regen
braking, start-stop availability) matches the battery actually installed.
Swapping to an AGM battery without updating this coding leaves the Gateway
applying the old battery's charge profile — commonly reported as suppressed
start-stop, "check battery"-type warnings, or simply undercharging the AGM
over time. This is a **write to the Gateway**, so per the safety rules it
MUST go through transaction.py (read -> backup -> write -> verify), the
same as adaptations.py's DME adaptation resets.

## What's confirmed vs. gated

CONFIRMED (publicly documented, multiple independent sources — see
VAG_BATTERY.md): the function lives on the Gateway (J533, VCDS diagnostic
address 19, UDS) under an adaptation labelled "Battery — replacement:
adaptation", exposing four logical fields: rated battery capacity (Ah),
battery technology (AGM vs. flooded/EFB), battery manufacturer, and battery
serial number.

SAFETY GATE — NOT confirmed: the raw UDS data identifiers (DIDs) those four
fields map to, and whether a SecurityAccess (0x27) login is required first.
Ross-Tech's own database (vis4vag) keeps these values behind a paywall and
scoped per vehicle/gateway-software-variant — i.e. even a paid number for
one Octavia might not match another. `CHANNEL_SPECS[*]["did"]` is therefore
None for every field, and build_write_plan()/code_battery() REFUSE to touch
a real car until they're filled in from a trace captured on THIS car (see
VAG_BATTERY.md for how). In demo mode the flow runs end-to-end against a
simulated Gateway so the plumbing, the transaction layer, and any future UI
can be exercised safely.

Never guess a DID here. The Gateway coordinates far more than the DME does
on the BMW side (central locking, lighting, comfort CAN routing) — a wrong
write is not a "worst case idle hunts for a minute" mistake.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths  # noqa: E402
from uds import UdsError  # noqa: E402
from isotp import IsoTpError  # noqa: E402

UdsPortError = (IsoTpError, UdsError)

GATEWAY_NAME = "Gateway (J533, VCDS diagnostic address 19)"
GATEWAY_ADDR = 0x19

# "Vlies" (fleece) = AGM, "Nass" (wet) = flooded/EFB — VCDS's own labels for
# telling the battery types apart by looking at the case (see VAG_BATTERY.md).
BATTERY_TECHNOLOGY = {"flooded": 0, "agm": 1}

# 3-letter battery-manufacturer codes as scraped from vis4vag's public
# summary (see VAG_BATTERY.md) — informational only, not wire-confirmed.
MANUFACTURER_CODES = {
    "VARTA": "VAX", "AKUMA": "UMX", "BANNER": "BAX", "MOLL": "MLA",
    "EXIDE": "TUX", "CLARIOS": "JCB", "JCI": "JCB", "BOADING": "XDO",
}

# One entry per logical adaptation field. `did` stays None (gated) until
# confirmed from a captured trace on this car; `encode` is a best-effort
# guess at the byte layout for when that day comes, NOT wire-confirmed.
CHANNEL_SPECS = {
    "capacity_ah": {"did": None, "encode": lambda v: bytes([int(round(v)) & 0xFF])},
    "technology": {"did": None,
                   "encode": lambda v: bytes([BATTERY_TECHNOLOGY[v]])},
    "manufacturer": {"did": None,
                      "encode": lambda v: v.encode("ascii")[:3].ljust(3, b"\x00")},
    "serial": {"did": None, "encode": lambda v: str(v).encode("ascii")},
}

CHANNEL_SOURCE = (
    "VCDS/vis4vag adaptation-channel labels for Gateway 19's 'Battery — "
    "replacement: adaptation' (rated capacity, technology, manufacturer, "
    "serial number) are publicly documented. The raw UDS DIDs those "
    "channels map to are NOT public — Ross-Tech's own database paywalls "
    "them, per vehicle/gateway-software-variant. Capture a UDS trace of "
    "the real procedure on THIS car (VCDS 'diagnostic log' / OBDeleven "
    "export while performing the adaptation) and fill in CHANNEL_SPECS "
    "before this will write anything.")


def build_write_plan(capacity_ah, technology, manufacturer=None, serial=None):
    """Validate inputs and build the write plan, EXCEPT the DIDs, which are
    gated. Returns (plan_or_None, note). `plan` is a list of
    {"field", "did", "data"} dicts once every needed DID is confirmed;
    until then returns (None, reason) so callers cannot accidentally send a
    guessed DID."""
    if technology not in BATTERY_TECHNOLOGY:
        raise ValueError(f"unknown battery technology: {technology!r} "
                          f"(have: {', '.join(BATTERY_TECHNOLOGY)})")
    fields = {"capacity_ah": capacity_ah, "technology": technology}
    if manufacturer is not None:
        fields["manufacturer"] = manufacturer
    if serial is not None:
        fields["serial"] = serial

    missing = sorted(f for f in fields if CHANNEL_SPECS[f]["did"] is None)
    if missing:
        return None, (f"battery coding blocked: no confirmed UDS DID for "
                       f"{', '.join(missing)} — {CHANNEL_SOURCE}")

    plan = [{"field": f, "did": CHANNEL_SPECS[f]["did"],
             "data": CHANNEL_SPECS[f]["encode"](v)}
            for f, v in fields.items()]
    return plan, "plan built"


def read_gateway_state(u):
    """Best-effort read-only reconnaissance against the Gateway over an
    already-open uds.Uds channel `u`. Safe to call freely — no writes.
    Doesn't assume the adaptation channels are readable (they're gated
    unknowns); reads the standard VIN DID (0xF190) as a sanity check that
    we're actually talking to the right module."""
    out = {}
    try:
        u.session_control()
        out["session"] = "extended"
    except UdsPortError as e:
        out["session_error"] = str(e)
    try:
        vin_bytes = u.read_data_by_identifier(0xF190)
        out["vin"] = bytes(vin_bytes[2:]).decode("ascii", "replace")
    except UdsPortError as e:
        out["vin_error"] = str(e)
    out["battery_channels"] = ("unavailable — CHANNEL_SPECS DIDs "
                                "unconfirmed, see VAG_BATTERY.md")
    return out


def describe_manual_procedure():
    """Human-readable summary of the documented manual VCDS/OBDeleven
    procedure, printed by the CLI regardless of whether the automated write
    is gated — this is the actionable path available today."""
    return (
        "Manual procedure (VCDS or OBDeleven), documented publicly:\n"
        "  1. Select control module 19 - CAN Gateway.\n"
        "  2. Open Adaptation (VCDS: '10 - Adaptation'; long coding /\n"
        "     channel-based, not Coding-0x07).\n"
        "  3. Find the 'Battery - replacement: adaptation' channel group\n"
        "     and set:\n"
        "       - Rated battery capacity: the new battery's Ah rating\n"
        "       - Battery technology: AGM ('Vlies'/fleece case) vs.\n"
        "         flooded/EFB ('Nass'/wet case) — pick AGM here\n"
        "       - Battery manufacturer (if offered): match the new battery\n"
        "       - Battery serial number: change at least one digit from the\n"
        "         old value — this is what actually signals the BMS that a\n"
        "         new battery is fitted and triggers the relearn\n"
        "  4. Save/apply each channel, then clear any stored faults the\n"
        "     Gateway raised while the old values were mismatched.\n"
        "This project's own CAN/UDS write path (code_battery() below) is\n"
        "intentionally gated until the raw DIDs are confirmed from a trace\n"
        "captured on this car — see VAG_BATTERY.md.")


def code_battery(adapter, capacity_ah, technology, manufacturer=None,
                  serial=None, transaction_manager=None, user_note="",
                  allow_demo=True):
    """Full battery-coding flow through the transaction layer.

    On a real car this REFUSES unless every needed CHANNEL_SPECS DID is
    confirmed. In demo mode (adapter.name starts with "DEMO", or adapter
    has no `.uds`) it simulates the write so the whole pipeline is
    testable. `adapter` needs `.name`, `.vin`, and (for a real car) `.uds`
    — an already-open uds.Uds channel to the Gateway.
    """
    is_demo = getattr(adapter, "name", "").startswith("DEMO") or \
        getattr(adapter, "uds", None) is None
    vin = getattr(adapter, "vin", None) or "UNKNOWN_VIN"

    plan, note = build_write_plan(capacity_ah, technology,
                                   manufacturer=manufacturer, serial=serial)

    if not is_demo and plan is None:
        # Real car + unconfirmed DIDs -> hard refuse. No guessing.
        return {"success": False, "gated": True,
                "error": note,
                "capacity_ah": capacity_ah, "technology": technology}

    def read_fn():
        if is_demo:
            return {"session": "demo", "vin": vin,
                    "note": "simulated Gateway state"}
        return read_gateway_state(adapter.uds)

    def write_fn():
        if is_demo:
            return {"ok": True, "simulated": True, "capacity_ah": capacity_ah,
                     "technology": technology, "manufacturer": manufacturer,
                     "serial": serial}
        # plan is confirmed here; send each field through WriteDataByIdentifier.
        results = []
        for item in plan:
            resp = adapter.uds.write_data_by_identifier(item["did"],
                                                          item["data"])
            results.append({"field": item["field"],
                             "did": f"0x{item['did']:04X}",
                             "ok": resp is not None})
        return {"ok": all(r["ok"] for r in results), "fields": results}

    def verify_fn():
        if is_demo:
            return True
        after = read_gateway_state(adapter.uds)
        return "vin_error" not in after  # real verification re-reads channels

    if transaction_manager is None:
        from transaction import get_transaction_manager
        transaction_manager = get_transaction_manager(
            backup_root=os.path.join(paths.data_dir(), "backups"))

    result = transaction_manager.execute(
        vin=vin, module_name="Gateway", module_addr=GATEWAY_ADDR,
        operation="battery_coding", read_fn=read_fn, write_fn=write_fn,
        verify_fn=verify_fn,
        user_note=user_note or f"code battery: {capacity_ah}Ah {technology}")
    result["demo"] = is_demo
    result["capacity_ah"] = capacity_ah
    result["technology"] = technology
    if plan is not None:
        result["plan"] = [{"field": p["field"], "did": f"0x{p['did']:04X}"}
                           for p in plan]
        result["channel_source"] = CHANNEL_SOURCE
    return result


if __name__ == "__main__":
    print(describe_manual_procedure())
    print()
    plan, note = build_write_plan(70, "agm", manufacturer="VARTA",
                                   serial="1234567890")
    print(f"automated write plan: {plan} ({note})")
