#!/usr/bin/env python3
"""Legacy SAE J1979 (\"OBD-II\") Mode 01/03/04/09 over ISO 15765-4 (CAN).

This is the legislated, universally-supported layer every OBD-II compliant
car has — including the Octavia — independent of any manufacturer-specific
UDS addressing. Reliable without needing to discover/guess VAG's proprietary
module map first: functional request 0x7DF, responders reply on
0x7E8-0x7EF (== their physical request ID + 8).

Only current-data (Mode 01), stored-DTC read (Mode 03), DTC clear (Mode 04,
gated — see vag_diag.py) and vehicle-info (Mode 09, VIN) are implemented.
"""
import time

from isotp import CF, FC, FF, SF

FUNCTIONAL_ID = 0x7DF
REQ_ID_MIN, REQ_ID_MAX = 0x7E0, 0x7E7
RESP_ID_MIN, RESP_ID_MAX = 0x7E8, 0x7EF

MODE_CURRENT_DATA = 0x01
MODE_STORED_DTC = 0x03
MODE_CLEAR_DTC = 0x04
MODE_VEHICLE_INFO = 0x09

PID_NAMES = {
    0x03: "fuel system status", 0x04: "engine load %",
    0x05: "coolant temp C", 0x06: "short fuel trim %",
    0x07: "long fuel trim %", 0x0B: "intake manifold pressure kPa",
    0x0C: "RPM", 0x0D: "speed km/h",
    0x0E: "timing advance deg", 0x0F: "intake air temp C",
    0x10: "MAF g/s", 0x11: "throttle %", 0x13: "O2 sensors present",
    0x1F: "run time s",
    0x21: "distance w/ MIL on km",
    0x33: "barometric pressure kPa",
    0x42: "control module V", 0x5C: "oil temp C",
    0x14: "O2 sensor 1 V", 0x15: "O2 sensor 2 V",
    0x16: "O2 sensor 3 V", 0x17: "O2 sensor 4 V",
    0x18: "O2 sensor 5 V", 0x19: "O2 sensor 6 V",
    0x1A: "O2 sensor 7 V", 0x1B: "O2 sensor 8 V",
}

# PID 0x03 fuel-system-status byte (bank 1) -> human text, per SAE J1979.
FUEL_SYSTEM_STATUS = {
    0x01: "open loop",
    0x02: "closed loop",
    0x04: "open loop (load/decel)",
    0x08: "open loop (fault)",
    0x10: "closed loop (O2 fault)",
}


def decode_fuel_system_status(byte):
    """PID 0x03 bank-1 status byte -> human text, or None if unrecognized."""
    return FUEL_SYSTEM_STATUS.get(byte)


def decode_pid(pid, data):
    """Decode a Mode 01 PID response payload (bytes after mode+pid echo)."""
    if pid == 0x04 and data:
        return data[0] * 100.0 / 255.0
    if pid == 0x05 and data:
        return data[0] - 40
    if pid in (0x06, 0x07) and data:
        return (data[0] - 128) * 100.0 / 128.0
    if pid in (0x0B, 0x33) and data:  # SAE J1979: 1 byte, 1:1 kPa
        return float(data[0])
    if pid == 0x0C and len(data) >= 2:
        return ((data[0] << 8) | data[1]) / 4.0
    if pid == 0x0D and data:
        return data[0]
    if pid == 0x0E and data:
        return data[0] / 2.0 - 64
    if pid == 0x0F and data:
        return data[0] - 40
    if pid == 0x10 and len(data) >= 2:
        return ((data[0] << 8) | data[1]) / 100.0
    if pid == 0x11 and data:
        return data[0] * 100.0 / 255.0
    if pid == 0x1F and len(data) >= 2:
        return (data[0] << 8) | data[1]
    if pid == 0x21 and len(data) >= 2:  # distance traveled with MIL on, km
        return (data[0] << 8) | data[1]
    if 0x14 <= pid <= 0x1B and data:  # SAE J1979: byte A = voltage/200, byte B (unused here) = per-bank STFT
        return round(data[0] / 200.0, 3)
    if pid == 0x42 and len(data) >= 2:
        return ((data[0] << 8) | data[1]) / 1000.0
    if pid == 0x5C and data:
        return data[0] - 40
    return data.hex()


def decode_dtc_2byte(hi, lo):
    """Classic SAE J2012 2-byte DTC -> Pxxxx/Cxxxx/Bxxxx/Uxxxx."""
    prefix = "PCBU"[(hi >> 6) & 0x03]
    return f"{prefix}{(hi >> 4) & 0x03}{hi & 0x0F:01X}{lo:02X}"


def _send_sf(port, can_id, payload):
    frame = bytes([SF << 4 | len(payload)]) + payload
    frame += b"\x00" * (8 - len(frame))
    port.send_frame(can_id, frame)


def request_functional(port, mode, pid=None, timeout=0.6, stop_ids=None):
    """Broadcast a Mode 0x0N request and collect every responder's payload
    (mode+pid echo stripped), reassembling multi-frame responses. Returns
    {responder_can_id: payload_bytes}.

    stop_ids: optional set of responder CAN IDs -- if given, return as
    soon as every one of them has a complete result, instead of always
    waiting out the full timeout to give every possible responder a
    chance to answer (the default, still used when stop_ids is None --
    read_dtcs()/read_vin()/supported_pids_by_responder() want a complete
    picture across every ECU and aren't called in a hot loop, so they
    keep that behavior). Lets a caller that already knows which single
    ECU it cares about (live_sample() polling a known responder every
    cycle) skip waiting on responders it doesn't need: confirmed live on
    the Octavia that without this, every single-PID round trip took the
    entire timeout (0.301s measured for timeout=0.3) even though both the
    engine and transmission ECUs had already answered within
    milliseconds -- this is what made live_sample() polling ~15 channels
    per cycle so slow, not any actual bus/hardware limit."""
    positive_sid = mode + 0x40
    skip = 1 if pid is None else 2
    port.drain_rx()
    _send_sf(port, FUNCTIONAL_ID, bytes([mode]) if pid is None
             else bytes([mode, pid]))
    results = {}
    pending = {}  # resp_id -> [total_len, bytearray, next_seq]
    deadline = time.time() + timeout
    while time.time() < deadline or pending:
        if stop_ids and not pending and stop_ids <= results.keys():
            break
        remaining = max(0.0, deadline - time.time()) if time.time() < \
            deadline else 0.3
        f = port.recv_frame(remaining)
        if not f:
            if not pending:
                break
            continue
        can_id, data, _ = f
        if not (RESP_ID_MIN <= can_id <= RESP_ID_MAX) or not data:
            continue
        pci = data[0] >> 4
        if pci == SF:
            length = data[0] & 0x0F
            body = data[1:1 + length]
            if body and body[0] == positive_sid:
                results[can_id] = body[skip:]
        elif pci == FF:
            total = ((data[0] & 0x0F) << 8) | data[1]
            pending[can_id] = [total, bytearray(data[2:8]), 1]
            port.send_frame(can_id - 8, bytes([FC << 4, 0x00, 0x00])
                             + b"\x00" * 5)
        elif pci == CF and can_id in pending:
            total, buf, seq = pending[can_id]
            if (data[0] & 0x0F) != seq:
                del pending[can_id]
                continue
            buf += data[1:]
            if len(buf) >= total:
                body = bytes(buf[:total])
                if body and body[0] == positive_sid:
                    results[can_id] = body[skip:]
                del pending[can_id]
            else:
                pending[can_id] = [total, buf, (seq + 1) & 0x0F]
    return results


def parse_dtc_response(body):
    """Mode 03 positive-response body (everything after the 0x43 SID byte):
    a 1-byte DTC count followed by 2-byte DTC pairs, per SAE J2012. This
    payload shape is transport-agnostic — same bytes whether they arrived
    via ISO-TP/CAN (request_functional, below) or a single K-line frame
    (power_diag.KLine) — so every transport shares this one decoder."""
    codes = []
    pairs = body[1:] if body else b""  # first byte is the DTC count, not data
    for i in range(0, len(pairs) - 1, 2):
        hi, lo = pairs[i], pairs[i + 1]
        if hi == 0 and lo == 0:
            continue
        codes.append(decode_dtc_2byte(hi, lo))
    return codes


def parse_vin_response(body):
    """Mode 09 PID 0x02 positive-response body (everything after the
    0x49 0x02 SID+PID echo): a 1-byte item count + 17 ASCII VIN characters.
    Transport-agnostic like parse_dtc_response — shared by CAN and K-line."""
    text = bytes(b for b in body[1:] if 0x20 <= b < 0x7F).decode(
        "ascii", "ignore")
    return text if len(text) >= 11 else None


def read_dtcs(port, timeout=1.0):
    """Mode 03 — stored DTCs from every responder. Returns
    {responder_can_id: [dtc_string, ...]}."""
    raw = request_functional(port, MODE_STORED_DTC, timeout=timeout)
    return {can_id: parse_dtc_response(body) for can_id, body in raw.items()}


def clear_dtcs(port, timeout=2.0):
    """Mode 04 — clear stored DTCs on every responder. Caller must gate
    this behind explicit user confirmation (see vag_diag.py mode_clear)."""
    return request_functional(port, MODE_CLEAR_DTC, timeout=timeout)


def read_vin(port, timeout=1.0):
    raw = request_functional(port, MODE_VEHICLE_INFO, pid=0x02,
                              timeout=timeout)
    for body in raw.values():
        vin = parse_vin_response(body)
        if vin:
            return vin
    return None


def read_pid(port, pid, timeout=0.6, stop_ids=None):
    return request_functional(port, MODE_CURRENT_DATA, pid=pid,
                               timeout=timeout, stop_ids=stop_ids)


def supported_pids_by_responder(port, timeout=0.6):
    """{responder_can_id: [pid, ...]} -- walk PIDs 0x00/0x20/0x40/... per
    responding ECU separately, since more than one module can answer the
    functional broadcast (confirmed live on the Octavia: 0x7E8 engine,
    0x7E9 transmission, each supporting a different PID set). Same
    algorithm as iso9141.supported_pids(), which needs this per-source
    breakdown for the same reason -- more than one ECU on that bus too."""
    result = {}
    base = 0x00
    while True:
        raw = request_functional(port, MODE_CURRENT_DATA, pid=base,
                                  timeout=timeout)
        if not raw:
            break
        more = False
        for can_id, data in raw.items():
            if len(data) < 4:
                continue
            bits = int.from_bytes(data[:4], "big")
            pids = result.setdefault(can_id, set())
            for i in range(32):
                if bits & (1 << (31 - i)):
                    pids.add(base + i + 1)
            if bits & 1:
                more = True
        if not more or base >= 0xE0:
            break
        base += 0x20
    return {can_id: sorted(pids) for can_id, pids in result.items()}


def supported_pids(port, timeout=0.6):
    """PIDs 0x00/0x20/0x40/... return a 4-byte bitmap of the next 32 PIDs
    supported. Walk it until a responder stops advertising a further
    range."""
    supported = set()
    base = 0x00
    while True:
        raw = request_functional(port, MODE_CURRENT_DATA, pid=base,
                                  timeout=timeout)
        if not raw:
            break
        bitmap = next(iter(raw.values()))
        if len(bitmap) < 4:
            break
        bits = int.from_bytes(bitmap[:4], "big")
        found_more = False
        for i in range(32):
            if bits & (1 << (31 - i)):
                pid = base + i + 1
                supported.add(pid)
                if i == 31:
                    found_more = True
        if not found_more:
            break
        base += 0x20
        if base > 0xC0:
            break
    return sorted(supported)
