#!/usr/bin/env python3
"""OVPF cloud sync client -- pushes local passport events to an OVP
provider (default: https://passport.skoor.ee, the same one the
separate viewer web app talks to). Stdlib-only (urllib), matching the
rest of this repo.

Auth mirrors the viewer's model exactly (confirmed against its live
JS -- there is no local copy of that app to read from, this was
reverse-derived from the deployed page):
  - personal identity: any email, OTP-verified, satisfies the
    provider's base write gate -- no pre-existing account needed.
  - workshop identity: OTP-verified against a DNS-verified domain,
    gets the "verified workshop" stamp on synced events whose
    producer.domain matches. Requires the domain to already be
    verified with the provider -- this client can *check*
    verification status but can't add a DNS record for you.

Sessions live in cloud_session.json (gitignored, like hidden_vehicles.json) --
tokens never committed, never synced anywhere else.

The provider's passport id is a bare UUID (its viewer mints one via
crypto.randomUUID()); our local vehicle URN is `urn:ovpf:<uuid>` (see
ovpf_producer.ensure_passport) -- the prefix must be stripped before
talking to the provider, see _bare_id().
"""
import json
import os
import shutil
import ssl
import urllib.error
import urllib.request

import ovpf_core
import ovpf_producer
import paths

PROVIDER_URL = os.environ.get("OVP_PROVIDER_URL", "https://passport.skoor.ee")


def _ssl_context():
    """A frozen (PyInstaller) build has no access to the OS certificate
    store the way a source-run interpreter does -- urlopen() then fails
    every HTTPS request with CERTIFICATE_VERIFY_FAILED.

    Prefer the CA bundle garagediag.spec copies in as a plain resource file
    (read back via paths.resource_dir(), same mechanism as ui.html) over
    calling certifi.where() directly: a pure-Python package's own
    __file__-relative path resolution isn't reliable once PyInstaller has
    packed it into its zipped archive. Falls back to an installed
    certifi, then the platform default (source runs, where it already
    works without any of this)."""
    bundled = os.path.join(paths.resource_dir(), "cacert.pem")
    if os.path.exists(bundled):
        return ssl.create_default_context(cafile=bundled)
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


_SSL_CONTEXT = _ssl_context()


class CloudError(Exception):
    pass


# --- local session storage --------------------------------------------------

def _session_path():
    return os.path.join(paths.data_dir(), "cloud_session.json")


def _load_sessions():
    try:
        with open(_session_path(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_sessions(sessions):
    with open(_session_path(), "w", encoding="utf-8") as f:
        json.dump(sessions, f)


def get_user_session():
    return _load_sessions().get("user")


def set_user_session(email, token):
    s = _load_sessions()
    s["user"] = {"email": email, "token": token}
    _save_sessions(s)
    return s["user"]


def clear_user_session():
    s = _load_sessions()
    s.pop("user", None)
    _save_sessions(s)


def get_workshop_session():
    return _load_sessions().get("workshop")


def set_workshop_session(domain, token, extra=None):
    s = _load_sessions()
    s["workshop"] = {"domain": domain, "token": token, **(extra or {})}
    _save_sessions(s)
    return s["workshop"]


def clear_workshop_session():
    s = _load_sessions()
    s.pop("workshop", None)
    _save_sessions(s)


def best_auth_session(producer_domain=None):
    """Whichever session can authenticate a write right now -- prefers a
    workshop session when it matches the event's own producer.domain
    claim, otherwise falls back to the personal identity. None if
    neither is signed in."""
    ws = get_workshop_session()
    if producer_domain and ws and ws.get("domain") == producer_domain:
        return ws
    us = get_user_session()
    if us:
        return us
    return ws


# --- HTTP -------------------------------------------------------------------

# Cloudflare's bot protection in front of the provider 403s/1010s urllib's
# default "Python-urllib/x.y" User-Agent -- needs to look like a real
# browser request to get through at all. Shared by _request() and
# pull_passport() (the latter can't reuse _request() itself -- /export
# returns raw NDJSON, not a single JSON body).
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/120.0 Safari/537.36")


def _request(method, path, body=None, token=None, timeout=10.0):
    url = PROVIDER_URL.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    req.add_header("User-Agent", _USER_AGENT)
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_SSL_CONTEXT) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return e.code, {"error": raw.decode("utf-8", "ignore") or f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        raise CloudError(f"provider unreachable: {e.reason}")


def _check(status, body):
    if status >= 300:
        raise CloudError(body.get("error", f"HTTP {status}"))
    return body


# --- personal OTP sign-in ---------------------------------------------------

def request_otp(email):
    return _check(*_request("POST", "/v1/auth/otp/request", {"email": email}))


def verify_otp(email, code):
    body = _check(*_request("POST", "/v1/auth/otp/verify",
                             {"email": email, "code": code}))
    set_user_session(email, body["token"])
    return body


# --- workshop OTP sign-in ---------------------------------------------------

def check_workshop_verified(domain):
    return _check(*_request("POST", f"/v1/workshops/{domain}/verify"))


def request_workshop_otp(domain, email):
    return _check(*_request(
        "POST", f"/v1/workshops/{domain}/otp/request", {"email": email}))


def verify_workshop_otp(domain, email, code):
    body = _check(*_request(
        "POST", f"/v1/workshops/{domain}/otp/verify",
        {"email": email, "code": code}))
    set_workshop_session(domain, body["token"],
                         {"role": body.get("role"),
                          "mechanicId": body.get("mechanicId"), "email": email})
    return body


# --- passport sync -----------------------------------------------------------

def _bare_id(vehicle_urn):
    """`urn:ovpf:<uuid>` -> `<uuid>` -- the provider's id format."""
    prefix = "urn:ovpf:"
    return vehicle_urn[len(prefix):] if vehicle_urn.startswith(prefix) else vehicle_urn


def pull_passport(passport_id):
    """Fetch a passport's full event history from the cloud provider and
    cache it locally as a new .ovpf.ndjson, keyed by the provider's own
    uuid rather than a VIN -- this device may have no VIN on file for a
    passport it's never seen before (e.g. one opened here by pasting/
    scanning a passport.skoor.ee URL for a car diagnosed on a different
    device). This is what makes diag_ui.py's ?urn=/?vin= passport lookup
    (see _qs_passport_state) work for a passport that only exists in the
    cloud so far -- without this it 404s locally even though the id is
    real, which is confusing since the exact same URL works fine in a
    browser (the web viewer always reads from the cloud).

    No-op if already cached locally -- doesn't clobber local data (which
    may have edits not yet pushed) with a possibly-stale cloud copy.
    Raises CloudError if the provider has nothing for this id."""
    path = os.path.join(ovpf_producer._passports_dir(),
                        f"{passport_id}.ovpf.ndjson")
    if os.path.exists(path):
        return path
    url = PROVIDER_URL.rstrip("/") + f"/v1/passports/{passport_id}/export"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=10.0,
                                    context=_SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise CloudError("no such passport on this provider")
        raise CloudError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise CloudError(f"provider unreachable: {e.reason}")
    if not raw.strip():
        raise CloudError("passport has no events yet")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw if raw.endswith("\n") else raw + "\n")
    # Every event just came *from* this provider -- mark them all synced
    # immediately, or sync_status/push_passport would see a brand new
    # local file with no .ovpf.synced.json sibling yet and think all of
    # them are pending, re-pushing everything right back. Found live: a
    # real passport's event count doubled 40 -> 80 this way (the provider
    # has since been hardened to be idempotent by event id too, but this
    # client shouldn't be relying on that as its only safeguard).
    try:
        ids = {json.loads(line)["id"] for line in raw.splitlines() if line.strip()}
        _save_synced_ids(path, ids)
    except Exception:
        pass
    return path


def _fetch_export(passport_id):
    url = PROVIDER_URL.rstrip("/") + f"/v1/passports/{passport_id}/export"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=10.0,
                                    context=_SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise CloudError("no such passport on this provider")
        raise CloudError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise CloudError(f"provider unreachable: {e.reason}")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def pull_and_merge_passport(vin, urn=None):
    """Fetch this passport's full remote event history and merge in any
    events that only exist there -- the counterpart to push_passport, for
    events created independently on the cloud side (e.g. a nickname edited
    via the web viewer, which writes straight to the provider). Unlike
    pull_passport() above (which only ever creates a brand-new local file,
    and is a no-op the moment one exists -- deliberately, to never clobber
    unpushed local edits), this always fetches and unions by event id
    (ovpf_core.merge) on top of whatever's local, since the whole point is
    picking up remote-only events on an *existing* history. Backs up the
    local file first (same .pre-*-backup precedent as the manual duplicate-
    passport merge) since this rewrites it in place, then re-seals the
    combined chain. Returns {"added", "conflicts"}; conflicts (same event
    id, different content -- shouldn't happen with real usage, ids are
    minted once) are reported rather than silently resolved either way.
    Raises CloudError if there's no local passport to merge into, the
    provider has nothing for this id, or is unreachable."""
    path = ovpf_producer.resolve_log_path(vin, urn)
    local_events = ovpf_core.load(path)
    if not local_events:
        return {"added": 0, "conflicts": [],
                "note": "no local passport for this VIN"}
    passport_id = _bare_id(local_events[0]["vehicle"])
    # Fetch before taking the lock -- a slow network round trip has no
    # business holding up a concurrent local writer (e.g. a live pull
    # finishing and appending its own DynoRun event).
    remote_events = _fetch_export(passport_id)

    # Everything from here reads and rewrites the file, so it needs the
    # same lock append() takes (ovpf_core._file_lock) -- this used to do a
    # plain read-modify-write with no locking at all, which could silently
    # drop a concurrent append landing between the read above and the
    # write below (this device's local_events snapshot could already be
    # stale by the time this runs). Local state is re-read fresh inside
    # the lock so the merge decision is made against what's actually on
    # disk right now, not the possibly-stale copy read a moment ago.
    with ovpf_core._file_lock(path):
        local_events = ovpf_core.load(path)
        local_ids = {ev["id"] for ev in local_events}
        new_events = [ev for ev in remote_events if ev["id"] not in local_ids]
        # merge() (and its conflict detection) has to run even when there
        # are no brand-new ids -- a remote copy of an *existing* event
        # with different content is exactly the conflict case, and it
        # wouldn't show up in new_events at all (same id, so it'd get
        # silently missed by an early-return here rather than reported).
        merged, conflicts = ovpf_core.merge(local_events, remote_events)
        if not new_events:
            return {"added": 0, "conflicts": conflicts}
        sealed = ovpf_core.seal(merged)
        shutil.copy2(path, path + ".pre-pull-backup")
        with open(path, "w", encoding="utf-8") as f:
            for ev in sealed:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        # New events came *from* the provider -- mark them synced
        # immediately, same reasoning as pull_passport above, so
        # push_passport doesn't try to re-upload them right back.
        synced = _load_synced_ids(path)
        synced.update(ev["id"] for ev in new_events)
        _save_synced_ids(path, synced)
    return {"added": len(new_events), "conflicts": conflicts}


def _synced_path(log_path):
    """Sibling of a resolved passport log path -- takes the actual path, not
    a vin, so it always matches whichever file resolve_log_path() picked
    (a passport opened before its VIN was known lives under "unknown", and
    re-deriving from vin here would silently point at a different, empty
    file -- see resolve_log_path)."""
    base = log_path[:-len(".ovpf.ndjson")] if log_path.endswith(".ovpf.ndjson") \
        else log_path
    return base + ".ovpf.synced.json"


def _load_synced_ids(log_path):
    try:
        with open(_synced_path(log_path), encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_synced_ids(log_path, ids):
    with open(_synced_path(log_path), "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


def ensure_registered(passport_id, session):
    status, body = _request("POST", "/v1/passports", {"id": passport_id},
                            token=session["token"])
    if status in (200, 201, 409):
        return True
    raise CloudError(body.get("error", f"HTTP {status}"))


def sync_status(vin, urn=None):
    """Local-only summary (no network) -- how many events are queued to
    push next time, for the UI to show before the user commits to it.
    Optional urn resolves a passport opened before its VIN was known (see
    ovpf_producer.resolve_log_path) -- needed when browsing a garage
    vehicle that isn't the connected car."""
    path = ovpf_producer.resolve_log_path(vin, urn)
    events = ovpf_core.load(path)
    synced = _load_synced_ids(path)
    pending = [ev for ev in events if ev["id"] not in synced]
    return {"total": len(events), "synced": len(events) - len(pending),
            "pending": len(pending),
            "signed_in": bool(get_user_session() or get_workshop_session())}


def push_passport(vin, urn=None):
    """Push every not-yet-synced event for this VIN's local passport to
    the cloud provider. Returns {"pushed", "skipped", "errors"}.
    Raises CloudError("not signed in...") if nobody's authenticated --
    same gate the viewer's ensureRegistered() enforces. See sync_status()
    for the urn parameter."""
    path = ovpf_producer.resolve_log_path(vin, urn)
    events = ovpf_core.load(path)
    if not events:
        return {"pushed": 0, "skipped": 0, "errors": [],
                "note": "no local passport for this VIN"}
    passport_id = _bare_id(events[0]["vehicle"])
    synced = _load_synced_ids(path)
    to_send = [ev for ev in events if ev["id"] not in synced]
    if not to_send:
        return {"pushed": 0, "skipped": len(events), "errors": []}

    session = best_auth_session(to_send[0].get("producer", {}).get("domain"))
    if not session:
        raise CloudError("not signed in -- sign in (personal or workshop) "
                         "before pushing")

    # A producer.type "Workshop" event has no code path that creates it
    # locally anymore (the self-asserted local Workshop identity was
    # removed as confusing UX), but a hand-edited or pre-removal
    # .ovpf.ndjson file could still carry one. Uploading that claim as-is
    # would put an unverified "Workshop: <name> @ <domain>" stamp on a
    # cloud passport, which reads as a real workshop to anyone viewing it.
    # Only push it if there's a currently signed-in cloud workshop session
    # (real OTP-verified against that DNS-verified domain) backing the same
    # domain -- otherwise refuse the whole batch rather than silently
    # uploading an unverifiable claim.
    ws_session = get_workshop_session()
    for ev in to_send:
        producer = ev.get("producer", {})
        if producer.get("type") != "Workshop":
            continue
        domain = producer.get("domain")
        if not (domain and ws_session and ws_session.get("domain") == domain):
            raise CloudError(
                f"events are attributed to self-asserted workshop "
                f"'{producer.get('name', '?')}'"
                f"{' @ ' + domain if domain else ''}, which isn't "
                "cloud-verified -- verify that workshop domain (sign in as "
                "workshop) or clear the local workshop identity before "
                "pushing")

    ensure_registered(passport_id, session)

    pushed, errors = 0, []
    for ev in to_send:
        sess = best_auth_session(ev.get("producer", {}).get("domain")) or session
        # Defense in depth on top of the Workshop-domain check above: no
        # local code path sets producer.verified today (see
        # ovpf_producer._producer / test_ovpf_producer's assertion that it
        # never appears), but nothing here should *trust* that stays true --
        # a hand-edited or corrupted local .ovpf.ndjson could carry one.
        # Only the provider gets to stamp "verified"; strip it from
        # whatever we're about to upload rather than forwarding it as-is.
        send_ev = ev
        if "verified" in ev.get("producer", {}):
            send_ev = dict(ev)
            send_ev["producer"] = {k: v for k, v in ev["producer"].items()
                                   if k != "verified"}
        status, body = _request("POST", f"/v1/passports/{passport_id}/events",
                                send_ev, token=sess["token"])
        if status in (200, 201, 409):
            # 409 = the provider already has this event (e.g. a previous
            # push succeeded server-side but crashed/errored locally
            # before the sync-tracking file was updated) -- same
            # idempotent handling as ensure_registered's 409. Marking it
            # synced here is what stops it being retried forever.
            synced.add(ev["id"])
            pushed += 1
        elif status in (401, 403):
            errors.append({"id": ev["id"], "error": "session expired/invalid"})
            break  # matches the viewer: stop, don't burn through the rest
        else:
            errors.append({"id": ev["id"],
                          "error": body.get("error", f"HTTP {status}")})
    _save_synced_ids(path, synced)
    return {"pushed": pushed, "skipped": len(events) - len(to_send),
            "errors": errors}
