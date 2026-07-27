#!/usr/bin/env python3
"""OVPF core — the one canonical implementation of the format.

Single source of truth for: canonicalization, event hashing, monotonic
UUIDv7 ids, the event envelope, lock-guarded append, and the fold
(seal/verify/merge/reduce). Both the reference CLI (`ovpf.py`) and the
diagnostic app's producer (`garagediag/ovpf_producer.py`, a vendored copy)
import this, so there is exactly one implementation of the wire format.

Stdlib only. Keep the two copies byte-identical (a golden-hash test guards
against drift).
"""
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

CONTEXT = "https://openvehiclepassport.org/ns/v0.1"
SPEC_VERSION = "0.1"


# --- canonicalization + hashing (pragmatic RFC 8785 JCS subset) ------------

def _canon_number(n):
    """RFC 8785 mandates ECMAScript's Number::toString for JSON numbers --
    critically, a whole-number float has no ".0": (45.0).toString() === "45"
    in JS, but Python's json.dumps(45.0) == "45.0". Left alone, a Python
    producer and a JS producer would hash the *same* logical event to
    *different* bytes the moment a number field (price, a float odometer)
    happens to be a whole number. This must match any other conformant
    implementation's number formatting, or cross-provider verification
    silently breaks -- see conformance/fixtures for the shared vectors."""
    if isinstance(n, int):
        return str(n)
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError(f"{n!r} has no canonical JSON representation")
    if n == int(n) and abs(n) < 1e21:
        return str(int(n))
    return repr(n)


def _canon(obj):
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, (int, float)):
        return _canon_number(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list):
        return "[" + ",".join(_canon(v) for v in obj) + "]"
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda kv: kv[0])
        return "{" + ",".join(
            json.dumps(k, ensure_ascii=False) + ":" + _canon(v) for k, v in items
        ) + "}"
    raise TypeError(f"{type(obj)} has no canonical JSON representation")


def canonicalize(obj):
    """Deterministic JSON bytes: sorted keys, compact separators, UTF-8,
    and number formatting that matches any ECMAScript-based implementation
    (see _canon_number)."""
    return _canon(obj).encode("utf-8")


def event_hash(event):
    """`sha256:<hex>` over the event with its own `hash` field removed."""
    body = {k: v for k, v in event.items() if k != "hash"}
    return "sha256:" + hashlib.sha256(canonicalize(body)).hexdigest()


# --- monotonic UUIDv7 ------------------------------------------------------

_uuid_lock = threading.Lock()
_uuid_last_ms = 0
_uuid_seq = 0


def uuid7():
    """RFC 9562 UUIDv7, monotonic: ms timestamp + 12-bit per-ms counter +
    62 random bits. Collision needs same ms + same counter + same random
    draw on two devices (~1/2^62)."""
    global _uuid_last_ms, _uuid_seq
    with _uuid_lock:
        ms = int(time.time() * 1000)
        if ms == _uuid_last_ms:
            _uuid_seq += 1
        else:
            _uuid_last_ms, _uuid_seq = ms, 0
        seq = _uuid_seq & 0xFFF
        rand_b = os.urandom(8)
    b = bytearray(ms.to_bytes(6, "big"))
    b.append(0x70 | (seq >> 8))
    b.append(seq & 0xFF)
    b += rand_b
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b[:16])))


def new_event_id():
    return "urn:uuid:" + uuid7()


def envelope(vehicle_urn, etype, data, producer, occurred_at=None):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "@context": CONTEXT,
        "id": new_event_id(),
        "type": etype,
        "specVersion": SPEC_VERSION,
        "vehicle": vehicle_urn,
        "occurredAt": occurred_at or now,
        "recordedAt": now,
        "producer": producer,
        "data": data,
    }


# --- storage: load + lock-guarded append -----------------------------------

@contextlib.contextmanager
def _file_lock(path, stale=10.0):
    """Portable advisory lock (a sibling .lock file). Breaks a stale lock
    left by a crashed writer after `stale` seconds."""
    lockpath = path + ".lock"
    while True:
        try:
            fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lockpath) > stale:
                    os.unlink(lockpath)
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lockpath)
        except OSError:
            pass


def load(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {e}")
    return events


def last_hash(path):
    last = None
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = line
    if last:
        try:
            return json.loads(last).get("hash")
        except json.JSONDecodeError:
            return None
    return None


def append(path, event):
    """Append one event, extending the hash-chain. Concurrency-safe:
    the read-last-hash + write is done under a file lock, so two producers
    can't fork the chain."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _file_lock(path):
        prev = last_hash(path)
        if prev:
            event["prevHash"] = prev
        event["hash"] = event_hash(event)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


# --- seal / verify / merge -------------------------------------------------

def seal(events):
    """Assign prevHash/hash in append order -> a valid chain."""
    prev = None
    for ev in events:
        ev.pop("hash", None)
        if prev is None:
            ev.pop("prevHash", None)
        else:
            ev["prevHash"] = prev
        ev["hash"] = event_hash(ev)
        prev = ev["hash"]
    return events


def verify_chain(events):
    """Return a list of problems ([] == intact), over file order."""
    problems, prev = [], None
    for i, ev in enumerate(events):
        eid = ev.get("id", f"#{i}")
        if "hash" not in ev:
            problems.append(f"{eid}: no hash (log not sealed)")
            prev = None
            continue
        if event_hash(ev) != ev["hash"]:
            problems.append(f"{eid}: hash mismatch (event altered)")
        if i == 0:
            if ev.get("prevHash"):
                problems.append(f"{eid}: genesis must not carry prevHash")
        elif ev.get("prevHash") != prev:
            problems.append(f"{eid}: broken link (prevHash != previous hash)")
        prev = ev["hash"]
    return problems


def _content(ev):
    return {k: v for k, v in ev.items() if k not in ("hash", "prevHash")}


def merge(*streams):
    """Union events by id (dedup); same id + different content -> conflict.
    Returns (merged_events, conflicts). Re-seal() the result."""
    by_id, conflicts = {}, []
    for stream in streams:
        for ev in stream:
            eid = ev.get("id")
            if eid in by_id:
                if canonicalize(_content(by_id[eid])) != canonicalize(_content(ev)):
                    conflicts.append(eid)
            else:
                by_id[eid] = ev
    merged = sorted(by_id.values(),
                    key=lambda e: (e.get("occurredAt", ""), e.get("id", "")))
    return merged, conflicts


# --- reduction: events -> derived state ------------------------------------

def _module(d):
    m = d.get("module") or {}
    return m.get("name") or m.get("address")


def _odometer_of(ev):
    d = ev.get("data", {})
    o = d.get("odometer")
    if isinstance(o, dict) and "value" in o:
        return o["value"], o.get("unit", "KMT")
    if ev.get("type") == "OdometerReading" and "value" in d:
        return d["value"], d.get("unit", "KMT")
    return None


def reduce(events):
    """Fold events (event-time order) into current state, applying
    corrections (an event named by a later `corrects` is skipped)."""
    corrected = {ev["corrects"] for ev in events if ev.get("corrects")}
    live = [ev for ev in events if ev.get("id") not in corrected]
    live.sort(key=lambda e: (e.get("occurredAt", ""), e.get("id", "")))

    state = {
        "passport": live[0].get("vehicle") if live else None,
        "vehicle": {}, "owner": None, "access": [], "mileage": None,
        "open_faults": [], "service_history": [], "coding_changes": [],
        "fuel_events": 0, "ai_insights": [], "timeline": [],
        "event_count": len(events), "corrections_applied": len(corrected),
    }
    faults = {}
    access = {}          # identity -> current role (the vehicle owns history;
    owner_id = None      # identities hold *temporal* access, not ownership)

    for ev in live:
        t, d = ev.get("type"), ev.get("data", {})
        p = ev.get("producer") or {}
        state["timeline"].append({
            "at": ev.get("occurredAt"), "type": t,
            "producer": p.get("name"),
            # Who was actually signed in and running the tool, distinct
            # from producer.name/type (the tool + mechanism, e.g. garagediag
            # + a Diagnostic ECU read) -- optional spec field (OVPF.md),
            # None for the common fully-anonymous case.
            "operator": p.get("operator"),
            # Domain-verified provenance (see docs/TRUST.md) -- stamped by
            # a provider onto the event at write time, never client-
            # asserted (see ovp-provider-aws/src/app.py's append_event).
            # Surfaced here so any UI can show a "verified workshop" badge
            # without re-deriving it from the raw producer object.
            "producerDomain": p.get("domain"),
            "producerVerified": bool(p.get("verified"))})

        od = _odometer_of(ev)
        if od and (state["mileage"] is None or od[0] > state["mileage"]["value"]):
            state["mileage"] = {"value": od[0], "unit": od[1]}

        if t == "PassportOpened":
            state["vehicle"].update(d.get("vehicle", {}))
        elif t == "VehicleIdentified":
            state["vehicle"].update(d.get("vehicle", d))
        elif t == "AccessGranted":
            ident, role = d.get("identity"), d.get("role", "viewer")
            if ident:
                access[ident] = role
                if role == "owner":
                    owner_id = ident
        elif t == "AccessRevoked":
            ident = d.get("identity")
            access.pop(ident, None)
            if ident == owner_id:
                owner_id = None
        elif t == "OwnershipTransferred":     # legacy alias -> owner-role grant
            if d.get("from"):
                access.pop(d["from"], None)
            if d.get("to"):
                access[d["to"]] = "owner"
                owner_id = d["to"]
        elif t == "DiagnosticTroubleCodeRead":
            mod = _module(d)
            # A full read of a module supersedes that module's previous
            # state, rather than accumulating into it forever -- a code
            # can legitimately disappear between scans (e.g. it self-
            # clears after enough healthy drive cycles) with no explicit
            # Cleared event ever logged for it. Without this, a fault
            # that resolved itself months ago stays "open" indefinitely
            # just because nobody ran the clear command on it specifically.
            # "since" carries forward from the prior read if the code is
            # still present (first-seen-in-this-streak, not
            # last-reconfirmed), reset only for a code that's newly
            # appeared here.
            prior = {k: v for k, v in faults.items() if k[0] == mod}
            for k in prior:
                del faults[k]
            for c in d.get("codes", []):
                key = (mod, c.get("code"))
                since = prior[key]["since"] if key in prior else ev.get("occurredAt")
                faults[key] = {"module": mod, "code": c.get("code"),
                                "text": c.get("text"), "since": since}
        elif t == "DiagnosticTroubleCodeCleared":
            mod = _module(d)
            cleared = d.get("codesCleared", [])
            if not cleared or "*" in cleared:
                for k in [k for k in faults if k[0] == mod]:
                    faults.pop(k, None)
            else:
                for code in cleared:
                    faults.pop((mod, code), None)
        elif t in ("ServicePerformed", "PartReplaced"):
            total = d.get("total") or {}
            state["service_history"].append({
                "date": (ev.get("occurredAt") or "")[:10],
                "type": d.get("serviceType", t),
                "total": total.get("price"), "currency": total.get("currency"),
                "odometer": (d.get("odometer") or {}).get("value")})
        elif t == "EcuCodingChanged":
            state["coding_changes"].append({
                "date": (ev.get("occurredAt") or "")[:10],
                "module": _module(d), "preset": d.get("preset"),
                "before": d.get("before"), "after": d.get("after")})
        elif t == "FuelAdded":
            state["fuel_events"] += 1
        elif t == "AiInsightGenerated":
            state["ai_insights"].append(
                {"insight": d.get("insight"), "confidence": d.get("confidence")})

    state["open_faults"] = list(faults.values())
    state["access"] = [{"identity": i, "role": r} for i, r in access.items()]
    state["owner"] = owner_id
    return state
