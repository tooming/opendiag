#!/usr/bin/env python3
"""Offline diagnostic report generator.

    python3 report.py drive_log_*.csv [--vin VIN] [--out NAME]

Reads already-recorded drive-log CSV(s) (see diag_ui.py's record_start())
plus this VIN's local OVPF passport (vehicle identity, stored fault codes
-- see ovpf_producer.py) and writes NAME.md + NAME.json: a vehicle header,
a fault-code table, and per-pull tables (peak RPM/MAF/throttle, estimated
power/torque, fuel-trim and environmental ranges) for each WOT pull found.

Deliberately data-only -- no "Interpretation"/"Recommended Next Steps"
prose. Producing that requires judgment calls a fixed script can't make
safely (this is exactly the E87-116i-vs-neighbour's-130i mix-up that
prompted the /api/report_metadata work in the first place -- see
CLAUDE.md). Paste the .md into an LLM chat for that part; it has every
number needed and none of the risk of a script quietly asserting a wrong
conclusion.

Pull boundaries come from the app's own pull_start/pull_end event markers
already written into the CSV by diag_ui.py's live detect_pull() -- this
deliberately doesn't re-detect WOT pulls independently, so the report's
numbers always match what the dashboard showed during the actual drive.

Pure stdlib, offline, no car needed.
"""
import argparse
import csv
import json
import os
import sys
import time

import paths  # noqa: E402
import ovpf_producer  # noqa: E402
from diag_ui import estimate_power_torque  # noqa: E402


def read_drive_log(path):
    """Returns (vin_or_None, rows) -- rows are dicts keyed by CSV header.
    vin comes from the "# vin: ..." comment line record_start() writes,
    when present (older recordings predate that feature -- see CLAUDE.md's
    two-E87 note for why this matters)."""
    with open(path, newline="") as f:
        first = f.readline()
        vin = None
        if first.startswith("# vin:"):
            vin = first.split(":", 1)[1].strip() or None
        else:
            f.seek(0)
        rows = list(csv.DictReader(f))
    return vin, rows


def _num(row, key):
    v = row.get(key)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _range(rows, key):
    vals = [v for r in rows if (v := _num(r, key)) is not None]
    return {"min": min(vals), "max": max(vals)} if vals else None


def _peak(rows, key):
    vals = [v for r in rows if (v := _num(r, key)) is not None]
    return max(vals) if vals else None


def find_pulls(rows):
    """Group rows into WOT pulls by matching pull_start/pull_end event
    markers (see module docstring) rather than re-deriving them."""
    starts = {}
    pulls = []
    for r in rows:
        ev, pid = r.get("event"), r.get("pull_id")
        if not pid:
            continue
        if ev == "pull_start":
            starts[pid] = _num(r, "epoch")
        elif ev == "pull_end" and pid in starts:
            t0 = starts.pop(pid)
            t1 = _num(r, "epoch")
            if t0 is None or t1 is None:
                continue
            window = [w for w in rows
                      if (e := _num(w, "epoch")) is not None and t0 <= e <= t1]
            pulls.append(summarize_pull(pid, t0, t1, window))
    return pulls


def summarize_pull(pull_id, t0, t1, window):
    peak_rpm = _peak(window, "rpm")
    peak_maf = _peak(window, "maf")
    power_kw = torque_nm = None
    if window:
        # power/torque at the sample with peak MAF, same convention as the
        # dashboard's own dyno tab (diag_ui.py's estimate_power_torque).
        best = max(window, key=lambda r: _num(r, "maf") or -1)
        power_kw, torque_nm = estimate_power_torque(
            _num(best, "maf"), _num(best, "rpm"))
    return {
        "pull_id": pull_id, "duration_s": round(t1 - t0, 1),
        "peak_rpm": peak_rpm, "peak_maf_gps": peak_maf,
        "peak_throttle_pct": _peak(window, "throttle"),
        "power_kw": power_kw, "torque_nm": torque_nm,
        "stft_range_pct": _range(window, "stft"),
        "ltft_range_pct": _range(window, "ltft"),
        "battery_v_range": _range(window, "battery_v"),
        "coolant_c_range": _range(window, "coolant_c"),
    }


def vehicle_and_faults_for_vin(vin):
    """Local-only lookup (no network) -- see ovpf_producer.list_passports().
    None if this VIN has no local passport yet."""
    if not vin:
        return None
    for state in ovpf_producer.list_passports(include_hidden=True):
        if (state.get("vehicle") or {}).get("vin") == vin:
            return {
                "vehicle": state.get("vehicle"),
                "nickname": state.get("name"),
                "mileage": state.get("mileage"),
                "open_faults": state.get("open_faults", []),
            }
    return None


def build_report(csv_paths, vin_override=None):
    files = []
    vins_seen = set()
    for path in csv_paths:
        vin, rows = read_drive_log(path)
        if vin:
            vins_seen.add(vin)
        files.append({"path": os.path.basename(path), "vin": vin,
                      "row_count": len(rows), "pulls": find_pulls(rows)})

    warnings = []
    if len(vins_seen) > 1:
        warnings.append(
            f"Input files disagree on VIN ({sorted(vins_seen)}) -- they "
            "may be from different cars. Not merging vehicle info; check "
            "which file belongs to which car before trusting this report.")

    vin = vin_override or (next(iter(vins_seen)) if len(vins_seen) == 1 else None)
    vf = vehicle_and_faults_for_vin(vin)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "app_version": paths.app_version(),
        "vin": vin,
        "vehicle": (vf or {}).get("vehicle"),
        "nickname": (vf or {}).get("nickname"),
        "mileage": (vf or {}).get("mileage"),
        "open_faults": (vf or {}).get("open_faults", []),
        "warnings": warnings,
        "files": files,
    }


def _fmt_range(r, unit=""):
    if not r:
        return "-"
    return f"{r['min']:.1f}{unit} .. {r['max']:.1f}{unit}"


def to_markdown(report):
    lines = ["# GarageDiag Diagnostic Report", ""]
    lines.append(f"Generated {report['generated_at']} "
                 f"(app `{report['app_version']}`)")
    lines.append("")
    if report["warnings"]:
        for w in report["warnings"]:
            lines.append(f"> **Warning:** {w}")
        lines.append("")

    lines.append("## Vehicle")
    v = report.get("vehicle") or {}
    if report["vin"]:
        lines.append(f"- VIN: `{report['vin']}`")
    if report.get("nickname"):
        lines.append(f"- Nickname: {report['nickname']}")
    make_model = " ".join(str(x) for x in
                          (v.get("make"), v.get("model"), v.get("modelYear"))
                          if x)
    if make_model:
        lines.append(f"- Make/Model/Year: {make_model}")
    if report.get("mileage"):
        m = report["mileage"]
        lines.append(f"- Mileage: {m['value']} {m['unit']}")
    if not (report["vin"] or make_model):
        lines.append("- Not identified (no VIN found in the input CSV(s); "
                     "pass --vin explicitly)")
    lines.append("")

    lines.append("## Stored Fault Codes")
    faults = report.get("open_faults") or []
    if faults:
        lines.append("| Module | Code | Description | Open since |")
        lines.append("|---|---|---|---|")
        for f in faults:
            lines.append(f"| {f.get('module', '')} | {f.get('code', '')} "
                         f"| {f.get('text', '')} | {f.get('since', '')} |")
    else:
        lines.append("No open faults on record for this VIN "
                     "(or no local passport found).")
    lines.append("")

    for f in report["files"]:
        lines.append(f"## Drive Log: `{f['path']}`")
        lines.append(f"{f['row_count']} rows, {len(f['pulls'])} WOT pull(s) "
                     "detected.")
        lines.append("")
        if f["pulls"]:
            lines.append("| Pull | Duration (s) | Peak RPM | Peak MAF (g/s) "
                         "| Peak Throttle % | Est. Power (kW) | "
                         "Est. Torque (Nm) | STFT range % | LTFT range % | "
                         "Battery V | Coolant C |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for p in f["pulls"]:
                lines.append(
                    f"| {p['pull_id']} | {p['duration_s']} "
                    f"| {p['peak_rpm'] or '-'} | {p['peak_maf_gps'] or '-'} "
                    f"| {p['peak_throttle_pct'] or '-'} "
                    f"| {p['power_kw'] or '-'} | {p['torque_nm'] or '-'} "
                    f"| {_fmt_range(p['stft_range_pct'])} "
                    f"| {_fmt_range(p['ltft_range_pct'])} "
                    f"| {_fmt_range(p['battery_v_range'])} "
                    f"| {_fmt_range(p['coolant_c_range'])} |")
        lines.append("")

    lines.append("---")
    lines.append("_Data tables only, mechanically derived from the sources "
                 "above -- no interpretation. Paste this file into an LLM "
                 "chat (Claude, ChatGPT, ...) for findings/analysis; every "
                 "number it needs is here._")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv_paths", nargs="*", help="drive-log CSV(s) to include")
    ap.add_argument("--vin", help="override/force the VIN (e.g. when the "
                    "CSV predates the '# vin:' header line)")
    ap.add_argument("--out", help="output basename (default: derived from "
                    "VIN + timestamp)")
    args = ap.parse_args()

    if not args.csv_paths and not args.vin:
        sys.exit("usage: report.py drive_log.csv [drive_log2.csv ...] "
                 "[--vin VIN] [--out NAME]\n"
                 "(need at least one CSV, or --vin for a fault-only report)")

    report = build_report(args.csv_paths, vin_override=args.vin)

    out = args.out or f"report_{report['vin'] or 'unknown'}_{time.strftime('%Y%m%d_%H%M%S')}"
    md_path, json_path = f"{out}.md", f"{out}.json"
    with open(md_path, "w") as f:
        f.write(to_markdown(report))
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    for w in report["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)


if __name__ == "__main__":
    main()
