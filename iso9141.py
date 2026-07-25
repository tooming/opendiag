#!/usr/bin/env python3
"""ISO 9141-2 K-line transport for legislated OBD-II (SAE J1979) -- the
framing predecessor to KWP2000 fast-init (the protocol power_diag.KLine's
request()/fast_init() speak, used by E39Adapter/E87Adapter/Obd2Adapter in
diag_ui.py). Confirmed live on a 1999 Porsche 996.1 (Motronic ME7.8): 5-baud
init at the standard OBD-II address 0x33 (power_diag.OBD_FUNCTIONAL) returns
key bytes 0x08 0x08, which is plain ISO 9141-2 -- not KWP2000 (whose key
bytes live in the 0x8x/0x94 range). Requests use a fixed 3-byte header
`[0x68, target, tester]` + payload + a running-sum checksum; unlike KWP2000
there's no length byte, so a response frame's end has to be found either by
an inter-byte silence gap or by matching the checksum against an expected
frame shape -- this module does both (see split_frames).

Reuses power_diag.KLine for the raw serial transport and its slow_init()
5-baud bit-banger (already generic to any address, not KWP-specific) --
only the post-init request/response framing below is new. Payload decode
(Mode 01 PID math, Mode 03 DTC pairs, Mode 09 VIN ASCII) is shared with the
CAN/UDS Octavia path via obd2.py -- see obd2.py's module docstring: those
functions are transport-agnostic, same bytes regardless of whether they
arrived over ISO-TP or a single K-line frame.

More than one module can answer a functional broadcast -- confirmed on the
996.1, where source address 0x11 (the real ME7.8) and 0x1A (near-empty
Mode 01 support) both replied to the same request -- so every function here
returns a dict keyed by the responding ECU's source address rather than
assuming a single responder, mirroring obd2.request_functional()'s shape
for the CAN transport.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402
from power_diag import KLine, now, hexs, OBD_FUNCTIONAL  # noqa: E402
import obd2  # noqa: E402

TESTER = 0xF1
# The framing target byte for a functional (all-ECUs) request. This is a
# different concept from the 5-baud init address (0x33, power_diag.
# OBD_FUNCTIONAL): 0x33 is bit-banged during init to wake up every ECU on
# the bus, while 0x6A is the fixed "to all OBD receivers" target byte SAE
# J1979 uses inside every subsequent request frame's header.
FUNCTIONAL_TARGET = 0x6A
RESP_FMT, RESP_TARGET = 0x48, 0x6B  # fixed response header bytes, per SAE


def checksum(b):
    return bytes([sum(b) & 0xFF])


def build_request(payload, target=FUNCTIONAL_TARGET):
    msg = bytes([0x68, target, TESTER]) + bytes(payload)
    return msg + checksum(msg)


def split_frames(raw, expect_sid=None, expect_pid=None):
    """Split (possibly concatenated, possibly multi-ECU) response bytes
    into individual frames: header `48 6B <src>`, then the response SID
    (mode + 0x40), then -- for Mode 01/09 -- the echoed PID, then data,
    then a checksum closing the frame. expect_sid/expect_pid narrow what
    counts as a real frame boundary considerably (both must match before
    a checksum is even attempted), which avoids the coincidental-checksum
    false positives a length-blind scan is prone to."""
    frames = []
    i, n = 0, len(raw)
    while i + 5 <= n:
        if raw[i] != RESP_FMT or raw[i + 1] != RESP_TARGET:
            i += 1
            continue
        sid_idx = i + 3
        if sid_idx >= n:
            break
        sid = raw[sid_idx]
        if expect_sid is not None and sid != expect_sid:
            i += 1
            continue
        data_start = sid_idx + 1
        if expect_pid is not None:
            if data_start >= n or raw[data_start] != expect_pid:
                i += 1
                continue
            data_start += 1
        found = False
        for end in range(data_start + 1, min(n, i + 16) + 1):
            seg = raw[i:end]
            if (sum(seg[:-1]) & 0xFF) == seg[-1]:
                frames.append({"src": raw[i + 2], "sid": sid,
                               "data": raw[data_start:end - 1], "raw": seg})
                i = end
                found = True
                break
        if not found:
            i += 1
    return frames


def request(kl, payload, target=FUNCTIONAL_TARGET, timeout=1.0,
            expect_sid=None, expect_pid=None):
    """Send one ISO9141-2 request and return the parsed response frame(s).
    Reaches into KLine's internals (buf/_pump/io) the same way ds2_diag.DS2
    does for its own non-KWP framing -- see ds2_diag.py's request()."""
    msg = build_request(payload, target)
    kl.io.flush_input()
    kl.buf = b""
    kl.log(">>", msg)
    kl.io.write(msg)
    kl.io.drain()
    deadline = now() + timeout
    while len(kl.buf.lstrip(b"\x00")) < len(msg) and now() < deadline:
        kl._pump(0.02)
    b = kl.buf.lstrip(b"\x00")
    if b[:len(msg)] == msg:
        kl.buf = b[len(msg):]
    last_len = -1
    while now() < deadline:
        kl._pump(0.05)
        if len(kl.buf) == last_len and kl.buf:
            break
        last_len = len(kl.buf)
    raw = kl.buf
    kl.buf = b""
    if raw:
        kl.log("<<", raw)
    return split_frames(raw, expect_sid, expect_pid)


def request_functional(kl, mode, pid=None, timeout=0.6):
    """Broadcast a Mode 0x0N request and return {src_addr: data_bytes} for
    every responder -- same shape/naming as obd2.request_functional() for
    the CAN transport."""
    payload = bytes([mode]) if pid is None else bytes([mode, pid])
    frames = request(kl, payload, timeout=timeout, expect_sid=mode + 0x40,
                     expect_pid=pid)
    return {f["src"]: f["data"] for f in frames}


def read_vin(kl, timeout=1.0):
    raw = request_functional(kl, 0x09, pid=0x02, timeout=timeout)
    for body in raw.values():
        vin = obd2.parse_vin_response(body)
        if vin:
            return vin
    return None


def read_dtcs(kl, timeout=1.0):
    raw = request_functional(kl, 0x03, timeout=timeout)
    return {src: obd2.parse_dtc_response(body) for src, body in raw.items()}


def clear_dtcs(kl, timeout=2.0):
    """Mode 04 -- clear stored DTCs on every responder. Caller must gate
    this behind explicit user confirmation, same as obd2.clear_dtcs()."""
    return request_functional(kl, 0x04, timeout=timeout)


def read_pid(kl, pid, timeout=0.4):
    return request_functional(kl, 0x01, pid=pid, timeout=timeout)


def supported_pids(kl, timeout=0.4):
    """{src: [pid, ...]} -- walk PIDs 0x00/0x20/0x40/... per responding
    ECU separately, since more than one module can answer (see module
    docstring)."""
    result = collections.defaultdict(set)
    base = 0x00
    while True:
        raw = read_pid(kl, base, timeout=timeout)
        if not raw:
            break
        more = False
        for src, data in raw.items():
            if len(data) < 4:
                continue
            bits = int.from_bytes(data[:4], "big")
            for i in range(32):
                if bits & (1 << (31 - i)):
                    result[src].add(base + i + 1)
            if bits & 1:
                more = True
        if not more or base >= 0xE0:
            break
        base += 0x20
    return {src: sorted(pids) for src, pids in result.items()}


# ---------------------------------------------------------------- CLI

def mode_scan(kl):
    kb = kl.slow_init(OBD_FUNCTIONAL)
    if not kb:
        sys.exit("No response to 5-baud init (ignition on?)")
    print(f"5-baud init key bytes: 0x{kb[0]:02X} 0x{kb[1]:02X}")
    time.sleep(0.3)
    vin = read_vin(kl)
    if vin:
        print(f"VIN: {vin}")
    pids = supported_pids(kl)
    if not pids:
        print("no responders to Mode 01 PID 0x00")
        return
    for src, plist in sorted(pids.items()):
        print(f"\n== 0x{src:02X} ({len(plist)} Mode-01 PIDs supported)")
        print("   " + " ".join(f"{p:02X}" for p in plist))
    dtcs = read_dtcs(kl)
    for src, codes in sorted(dtcs.items()):
        if codes:
            print(f"\n0x{src:02X} DTCs ({len(codes)}): {', '.join(codes)}")
        else:
            print(f"\n0x{src:02X} DTCs: none stored")


def mode_dtc(kl):
    kb = kl.slow_init(OBD_FUNCTIONAL)
    if not kb:
        sys.exit("No response to 5-baud init (ignition on?)")
    time.sleep(0.3)
    dtcs = read_dtcs(kl)
    if not dtcs:
        print("no responders to Mode 03")
        return
    for src, codes in sorted(dtcs.items()):
        if codes:
            print(f"0x{src:02X} DTCs ({len(codes)}): {', '.join(codes)}")
        else:
            print(f"0x{src:02X} DTCs: none stored")


def mode_clear(kl, yes):
    kb = kl.slow_init(OBD_FUNCTIONAL)
    if not kb:
        sys.exit("No response to 5-baud init (ignition on?)")
    time.sleep(0.3)
    before = read_dtcs(kl)
    for src, codes in sorted(before.items()):
        print(f"0x{src:02X} before: {', '.join(codes) if codes else 'none'}")
    if not yes:
        print("\nDry run -- pass --yes to actually clear (Mode 04).")
        return
    clear_dtcs(kl)
    time.sleep(0.5)
    kb = kl.slow_init(OBD_FUNCTIONAL)
    after = read_dtcs(kl) if kb else {}
    for src, codes in sorted(after.items()):
        print(f"0x{src:02X} after:  {', '.join(codes) if codes else 'none'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=None)
    ap.add_argument("--raw", action="store_true")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("scan", help="supported PIDs + VIN + DTCs per responder")
    sub.add_parser("dtc", help="Mode 03 stored fault codes per responder")
    p = sub.add_parser("clear", help="Mode 04 clear (dry-run unless --yes)")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    rawlog = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "kline_raw.log")
    kl = KLine(args.port, show_raw=args.raw, rawlog_path=rawlog)
    try:
        if args.mode == "scan":
            mode_scan(kl)
        elif args.mode == "dtc":
            mode_dtc(kl)
        elif args.mode == "clear":
            mode_clear(kl, args.yes)
    finally:
        kl.close()


if __name__ == "__main__":
    main()
