#!/usr/bin/env python3
"""UDS (ISO 14229) service layer, built on isotp.IsoTpChannel.

Started as read-only diagnostics (plus gated clear) mirroring power_diag.py's
KWP2000 service subset. Now also exposes the generic write-capable services
(SecurityAccess, WriteDataByIdentifier, RoutineControl) needed by callers
like vag_battery.py — these are protocol *primitives* only, safe to add
because they don't presume any car-specific data identifier or seed/key
algorithm. Callers are responsible for their own safety gate before ever
calling write_data_by_identifier()/routine_control() with a real DID/routine
on a real car — see vag_battery.py's CHANNEL_SPECS gate for the pattern.
"""
import time

from isotp import IsoTpChannel, IsoTpError

DIAGNOSTIC_SESSION_CONTROL = 0x10
TESTER_PRESENT = 0x3E
SECURITY_ACCESS = 0x27
READ_DTC_INFORMATION = 0x19
CLEAR_DIAGNOSTIC_INFORMATION = 0x14
READ_DATA_BY_IDENTIFIER = 0x22
WRITE_DATA_BY_IDENTIFIER = 0x2E
ROUTINE_CONTROL = 0x31
NEGATIVE_RESPONSE = 0x7F

# routineControl sub-functions
ROUTINE_START = 0x01
ROUTINE_STOP = 0x02
ROUTINE_REQUEST_RESULTS = 0x03

SESSION_DEFAULT = 0x01
SESSION_EXTENDED = 0x03

# reportDTCByStatusMask sub-function, mask 0xFF = any status bit set
REPORT_DTC_BY_STATUS_MASK = 0x02

NRC = {
    0x10: "generalReject", 0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported", 0x13: "incorrectMessageLength",
    0x14: "responseTooLong", 0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect", 0x24: "requestSequenceError",
    0x31: "requestOutOfRange", 0x33: "securityAccessDenied",
    0x35: "invalidKey", 0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired", 0x70: "uploadDownloadNotAccepted",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class UdsError(Exception):
    def __init__(self, sid, nrc):
        self.sid = sid
        self.nrc = nrc
        text = NRC.get(nrc, f"0x{nrc:02X}")
        super().__init__(f"negative response to SID 0x{sid:02X}: {text}")


class Uds:
    def __init__(self, port, tx_id, rx_id, extended_ids=False):
        self.chan = IsoTpChannel(port, tx_id, rx_id, extended_ids)
        self.tx_id = tx_id
        self.rx_id = rx_id

    def request(self, sid, data=b"", timeout=1.0, allow_pending=True):
        """Send one UDS service request, return the response payload
        (without the echoed/incremented SID byte). Raises UdsError on a
        negative response, transparently waits out 0x78 (response
        pending)."""
        self.chan.send(bytes([sid]) + data)
        deadline_extra = 5.0  # generous ceiling across repeated 0x78 waits
        start = time.time()
        while True:
            resp = self.chan.recv(timeout)
            if resp is None:
                raise IsoTpError(f"no response to SID 0x{sid:02X}")
            if resp[0] == NEGATIVE_RESPONSE:
                if len(resp) < 3:
                    raise UdsError(sid, 0)
                nrc = resp[2]
                if nrc == 0x78 and allow_pending and \
                        time.time() - start < deadline_extra:
                    continue
                raise UdsError(sid, nrc)
            if resp[0] == sid + 0x40:
                return resp[1:]
            # unrelated frame (e.g. late tester-present echo) — keep waiting
            if time.time() - start > deadline_extra:
                raise IsoTpError(f"unexpected response to SID 0x{sid:02X}: "
                                  f"{resp.hex()}")

    def session_control(self, session=SESSION_EXTENDED):
        return self.request(DIAGNOSTIC_SESSION_CONTROL, bytes([session]))

    def tester_present(self):
        return self.request(TESTER_PRESENT, bytes([0x00]))

    def read_dtc_by_status_mask(self, mask=0xFF):
        return self.request(READ_DTC_INFORMATION,
                             bytes([REPORT_DTC_BY_STATUS_MASK, mask]))

    def read_data_by_identifier(self, did):
        return self.request(READ_DATA_BY_IDENTIFIER,
                             bytes([did >> 8, did & 0xFF]))

    def clear_diagnostic_information(self, group=0xFFFFFF):
        data = bytes([(group >> 16) & 0xFF, (group >> 8) & 0xFF,
                       group & 0xFF])
        return self.request(CLEAR_DIAGNOSTIC_INFORMATION, data, timeout=3.0)

    def write_data_by_identifier(self, did, data):
        """WriteDataByIdentifier (0x2E). `data` must already be encoded to
        the byte layout the target DID expects — this layer knows nothing
        about what any given DID means. Raises UdsError (e.g.
        securityAccessDenied/requestOutOfRange) on a negative response."""
        return self.request(WRITE_DATA_BY_IDENTIFIER,
                             bytes([did >> 8, did & 0xFF]) + bytes(data),
                             timeout=2.0)

    def security_access(self, level, key_fn):
        """SecurityAccess (0x27): request a seed at `level` (odd sub-function,
        e.g. 0x01/0x03/0x05...), pass it to `key_fn(seed_bytes) -> key_bytes`,
        and send the key back at `level + 1`. Returns True once the ECU
        accepts the key. A seed of all-zero bytes means the ECU is already
        unlocked at this level — key_fn is not called in that case.

        `key_fn` carries the actual manufacturer seed/key algorithm, which
        this module deliberately does not implement — VAG's algorithms are
        proprietary and not published anywhere this project can point to
        (unlike e.g. adaptations.py's sourced RomRaider opcode). Raises
        UdsError (securityAccessDenied/invalidKey/exceedNumberOfAttempts) on
        a rejected key — note exceedNumberOfAttempts can impose an ECU-side
        lockout timer, so callers must not retry blindly."""
        seed = self.request(SECURITY_ACCESS, bytes([level]))
        if seed and any(b != 0 for b in seed[1:]):
            key = key_fn(bytes(seed[1:]))
            self.request(SECURITY_ACCESS, bytes([level + 1]) + bytes(key))
        return True

    def routine_control(self, sub_function, routine_id, data=b""):
        """RoutineControl (0x31): start/stop/requestResults a routine.
        `sub_function` is one of ROUTINE_START/ROUTINE_STOP/
        ROUTINE_REQUEST_RESULTS."""
        return self.request(ROUTINE_CONTROL,
                             bytes([sub_function, routine_id >> 8,
                                    routine_id & 0xFF]) + bytes(data),
                             timeout=2.0)


def decode_dtc_records(payload, three_byte=True):
    """ReadDTCInformation (0x19 0x02) response: after the status-availability
    byte, records of 3 bytes DTC + 1 byte status (typical UDS layout used
    by VAG). `payload` is what Uds.read_dtc_by_status_mask() returns, i.e.
    the response with the SID byte already stripped: [sub-function echo,
    DTCStatusAvailabilityMask, records...]. Returns
    [(dtc_int, status_byte), ...]."""
    if len(payload) < 2:
        return []
    body = payload[2:]  # skip sub-function echo + DTCStatusAvailabilityMask
    recs = []
    step = 4 if three_byte else 3
    for i in range(0, len(body) - step + 1, step):
        chunk = body[i:i + step]
        dtc = (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
        status = chunk[3] if three_byte else None
        recs.append((dtc, status))
    return recs


def dtc_to_text(dtc):
    """Best-effort SAE Pxxxx/Cxxxx/Bxxxx/Uxxxx rendering of a 3-byte UDS DTC
    (ISO 14229-1 Annex D: top two bits of the high byte select the letter,
    remaining bits + middle byte are the 4-digit code; low byte is a
    manufacturer-defined fault-type sub-code, not part of the SAE number).
    This is reliable for engine/emissions codes; treat it as cosmetic for
    body/chassis modules the same way BMW hex codes are — verify meaning
    from the raw hex, don't trust the rendering blindly."""
    hi = (dtc >> 16) & 0xFF
    mid = (dtc >> 8) & 0xFF
    prefix = "PCBU"[(hi >> 6) & 0x03]
    return f"{prefix}{hi & 0x3F:02X}{mid:02X}"
