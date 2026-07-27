#!/usr/bin/env python3
"""VW Group (Octavia Mk3/MQB and similar) diagnostics over a CAN-capable
USB adapter on macOS. Two adapter types are supported (--adapter, default
auto-detect): a CANable 2.0-class adapter running stock slcan firmware
(can_transport.SlcanPort), or a Waveshare USB-CAN-A, which is CH340-based
and speaks its own binary framing instead (can_transport_waveshare.
WaveshareCanPort) — see that module's docstring for the wire protocol.

The K+DCAN cable used elsewhere in this repo is K-line only and cannot
reach this car — see CLAUDE.md. This talks ISO 15765-4 (OBD-II on CAN,
500 kbit) + ISO 14229 (UDS) instead.

Modes:
  probe    verify the adapter + car are talking (Mode 01 PID 00 broadcast)
  pids     which Mode-01 PIDs the engine ECU supports
  vin      read VIN (Mode 09 PID 02)
  scan     Mode 03 stored DTCs from every OBD responder, plus an
           experimental UDS sweep for non-emissions modules (unverified
           addressing — see sweep)
  dtc      Mode 03 stored DTCs only (the guaranteed-safe layer)
  clear    Mode 04 clear DTCs; auto-snapshots fault memory first
  monitor  RPM/speed/coolant/voltage -> console + CSV, Ctrl+C to stop
  sweep    probe candidate UDS physical CAN ID pairs 0x700-0x7FF for any
           module that answers TesterPresent. EXPERIMENTAL: VAG's actual
           module addressing (often routed through the J533 gateway) isn't
           reliably known without the physical car — this just tells you
           which raw CAN IDs get a reply, the same way power_diag.py's
           K-line `sweep` finds BMW module addresses on an unknown car.

Use --raw to print every line/frame on the wire (format depends on the
adapter in use); all traffic is also appended to can_raw.log.
"""
import argparse
import csv
import datetime
import os
import sys
import time

from can_transport import SlcanPort, SlcanError, find_port as find_slcan_port
from can_transport_waveshare import (WaveshareCanPort, WaveshareCanError,
                                      find_port as find_waveshare_port)
import obd2
from isotp import IsoTpError
from uds import Uds, UdsError, decode_dtc_records, dtc_to_text

class CanAdapterError(Exception):
    pass


CanPortError = (SlcanError, WaveshareCanError, CanAdapterError)


def open_port(adapter="auto", port=None, bitrate=500000, show_raw=False,
              rawlog_path=None):
    """Open the right CAN transport for --adapter (or auto-detect between
    a CANable-class slcan adapter and a Waveshare USB-CAN-A, the same way
    this module's CLI does). Shared with diag_ui.py's VagAdapter so the
    hardware-selection logic lives in exactly one place. Raises
    CanAdapterError if the choice is ambiguous or nothing is plugged in,
    SlcanError/WaveshareCanError if the chosen adapter itself fails to
    open."""
    if adapter == "auto":
        has_slcan = bool(find_slcan_port())
        has_waveshare = bool(find_waveshare_port())
        if has_slcan and has_waveshare:
            raise CanAdapterError(
                "both a CANable-class (slcan) and a Waveshare USB-CAN-A "
                "adapter appear to be plugged in — pass adapter='slcan' or "
                "adapter='waveshare' to disambiguate")
        elif has_slcan:
            adapter = "slcan"
        elif has_waveshare:
            adapter = "waveshare"
        else:
            raise CanAdapterError(
                "no USB-CAN adapter found (looked for a CANable-class "
                "/dev/cu.usbmodem* device and a Waveshare USB-CAN-A CH340 "
                "device) — is it plugged in? the K+DCAN cable will not "
                "work here, it has no CAN transceiver")
    if adapter == "slcan":
        return SlcanPort(port, bitrate=bitrate, show_raw=show_raw,
                          rawlog_path=rawlog_path)
    return WaveshareCanPort(port, bitrate=bitrate, show_raw=show_raw,
                            rawlog_path=rawlog_path)

DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def mode_probe(port):
    raw = obd2.request_functional(port, obd2.MODE_CURRENT_DATA, pid=0x00,
                                   timeout=1.0)
    if not raw:
        print("No response to Mode 01 PID 00 broadcast — check ignition is "
              "on, adapter is wired to CAN-H/CAN-L (OBD pins 6/14), and "
              "the bitrate (500 kbit, standard for this platform).")
        return False
    for can_id in sorted(raw):
        print(f"0x{can_id:03X} responded — bus alive")
    return True


def mode_pids(port):
    pids = obd2.supported_pids(port)
    if not pids:
        print("no responders")
        return
    for pid in pids:
        name = obd2.PID_NAMES.get(pid, "")
        print(f"  01 {pid:02X}  {name}")


def mode_vin(port):
    vin = obd2.read_vin(port)
    print(vin or "no VIN response")


def mode_dtc(port):
    dtcs = obd2.read_dtcs(port)
    if not dtcs:
        print("no responders")
        return
    for can_id, codes in dtcs.items():
        print(f"0x{can_id:03X}: {len(codes)} DTC(s)")
        for c in codes:
            print(f"     {c}")


def mode_scan(port):
    mode_dtc(port)
    print("\n-- experimental UDS module sweep (unverified addressing) --")
    mode_sweep(port, quiet_no_dtc=True)


def _snapshot(dtcs):
    path = os.path.join(DATA_DIR, "fault_snapshots.log")
    with open(path, "a") as f:
        f.write(f"\n=== {datetime.datetime.now().isoformat()} "
                f"(vag_diag pre-clear snapshot) ===\n")
        for can_id, codes in dtcs.items():
            f.write(f"0x{can_id:03X}: {', '.join(codes) or '(none)'}\n")


def mode_clear(port):
    before = obd2.read_dtcs(port)
    _snapshot(before)
    print(f"snapshotted current fault memory to fault_snapshots.log")
    obd2.clear_dtcs(port)
    time.sleep(0.5)
    after = obd2.read_dtcs(port)
    for can_id in sorted(set(before) | set(after)):
        remaining = after.get(can_id, [])
        print(f"0x{can_id:03X}: {len(remaining)} DTC(s) remain")
        for c in remaining:
            print(f"     still stored: {c}")


def mode_sweep(port, lo=0x700, hi=0x7FF, quiet_no_dtc=False):
    found = []
    for req_id in range(lo, hi + 1):
        if obd2.REQ_ID_MIN <= req_id <= obd2.REQ_ID_MAX:
            continue  # already covered by the OBD functional layer
        rx_id = req_id + 8
        try:
            u = Uds(port, req_id, rx_id)
            u.tester_present()
            found.append(req_id)
            print(f"0x{req_id:03X} -> 0x{rx_id:03X}: responded", end="")
            try:
                recs = decode_dtc_records(u.read_dtc_by_status_mask())
                if recs:
                    print(f", {len(recs)} DTC(s):")
                    for dtc, status in recs:
                        print(f"     {dtc_to_text(dtc)} "
                              f"(raw {dtc:06X}, status 0x{status:02X})")
                else:
                    print(", no DTCs")
            except (IsoTpError, UdsError) as e:
                print(f" (DTC read failed: {e})")
        except (IsoTpError, UdsError):
            pass
        except CanPortError:
            raise
    if not found and not quiet_no_dtc:
        print("no additional responders found in the swept range")
    return found


def mode_monitor(port, args):
    path = os.path.join(
        DATA_DIR, f"vag_log_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
    fields = ["t", "rpm", "speed_kmh", "coolant_c", "voltage"]
    pid_map = {"rpm": 0x0C, "speed_kmh": 0x0D, "coolant_c": 0x05,
               "voltage": 0x42}
    print(f"logging to {path}, Ctrl+C to stop")
    t0 = time.time()
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        deadline = None if args.duration is None else t0 + args.duration
        try:
            while deadline is None or time.time() < deadline:
                row = {"t": round(time.time() - t0, 3)}
                for col, pid in pid_map.items():
                    raw = obd2.read_pid(port, pid, timeout=args.timeout)
                    val = None
                    for body in raw.values():
                        val = obd2.decode_pid(pid, body)
                        break
                    row[col] = val
                w.writerow(row)
                fh.flush()
                print(f"  rpm={row['rpm']} speed={row['speed_kmh']}km/h "
                      f"coolant={row['coolant_c']}C "
                      f"volt={row['voltage']}V", end="\r")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    print(f"\nsaved {path}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=None,
                    help="serial port (auto-detected if omitted)")
    ap.add_argument("--adapter", choices=["auto", "slcan", "waveshare"],
                    default="auto",
                    help="CAN adapter type: slcan (CANable-class, "
                    "stock slcan firmware) or waveshare (Waveshare "
                    "USB-CAN-A, CH340-based binary protocol). Default "
                    "auto-detects from what's plugged in.")
    ap.add_argument("--bitrate", type=int, default=500000,
                    help="CAN bitrate, default 500000 (standard for MQB)")
    ap.add_argument("--raw", action="store_true",
                    help="print raw wire traffic (SLCAN lines or "
                    "Waveshare frame bytes, depending on adapter)")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("probe")
    sub.add_parser("pids")
    sub.add_parser("vin")
    sub.add_parser("scan")
    sub.add_parser("dtc")
    sub.add_parser("clear", help="clear stored DTCs; snapshots first")
    p = sub.add_parser("sweep",
                       help="EXPERIMENTAL: probe 0x700-0x7FF for UDS modules")
    p.add_argument("--lo", type=lambda s: int(s, 16), default=0x700)
    p.add_argument("--hi", type=lambda s: int(s, 16), default=0x7FF)
    p = sub.add_parser("monitor")
    p.add_argument("--interval", type=float, default=0.2)
    p.add_argument("--timeout", type=float, default=0.4)
    p.add_argument("--duration", type=float, default=None)
    args = ap.parse_args()

    rawlog = os.path.join(DATA_DIR, "can_raw.log")
    try:
        port = open_port(args.adapter, args.port, bitrate=args.bitrate,
                         show_raw=args.raw, rawlog_path=rawlog)
    except CanPortError as e:
        sys.exit(str(e))

    try:
        if args.mode == "probe":
            mode_probe(port)
        elif args.mode == "pids":
            mode_pids(port)
        elif args.mode == "vin":
            mode_vin(port)
        elif args.mode == "scan":
            mode_scan(port)
        elif args.mode == "dtc":
            mode_dtc(port)
        elif args.mode == "clear":
            mode_clear(port)
        elif args.mode == "sweep":
            mode_sweep(port, args.lo, args.hi)
        elif args.mode == "monitor":
            mode_monitor(port, args)
    finally:
        port.close()


if __name__ == "__main__":
    main()
