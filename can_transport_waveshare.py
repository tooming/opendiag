#!/usr/bin/env python3
"""Waveshare USB-CAN-A transport, for the Skoda Octavia (and other CAN/UDS
cars) on macOS — an alternative to can_transport.py's SlcanPort for people
whose adapter isn't a CANable-class slcan device.

This adapter is CH340/CH341-based (USB-UART bridge, vendor 0x1A86) driving
an onboard STM32F103 that does the actual CAN work, and it does NOT speak
slcan — it has its own binary framing. Protocol reference (Waveshare's own
"Secondary Development" doc, https://www.waveshare.com/wiki/
Secondary_Development_Serial_Conversion_Definition_of_CAN_Protocol),
confirmed against this project's real car (Mode 01 PID 00 broadcast to
0x7DF got genuine 0x7E8/0x7E9 responses back):

Serial port: fixed 2,000,000 baud, 8N1 (the CAN bitrate is a separate,
independently-configured value — see BITRATE_CODES).

CAN config command (20 bytes, sent once at open — no documented ack):
    AA 55 <type> <can_baud> <frame_type> <filter x4> <mask x4> <mode>
    <auto-retransmit> <backup x4> <checksum>
  type=0x12 selects the variable-length data protocol (vs 0x02 for a fixed
  20-byte variant this module doesn't use). frame_type/mode/backup fields
  are documented in the wiki page above. filter=mask=0 passes everything
  through unfiltered (confirmed empirically — both a real OBD-II response
  and unrelated adapter noise came through with these all zero).
  checksum = low byte of sum(bytes[2:19]).

Variable-length data frame:
    AA <type> <id bytes> <data 0-8 bytes> 55
  type: bit5 std(0)/ext(1), bit4 data(0)/remote(1), bits0-3 = data length.
  id bytes: 2 bytes little-endian (std, 11 bits) or 4 bytes little-endian
  (ext, 29 bits).

The adapter free-runs a repeating stray extended frame (ID 0x17F00010) in
the background regardless of mode/config — confirmed present with nothing
wired to CAN-H/L and unaffected by a factory reset. Harmless: it's an
extended frame and every real ID this project cares about (legislated
OBD-II, VAG UDS) is a standard 11-bit ID, so normal ID filtering in
isotp.py/obd2.py/uds.py discards it like any other irrelevant bus frame.
"""
import fcntl
import glob
import os
import re
import select
import struct
import subprocess
import sys
import termios
import time

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import serial
    PORT_ERRORS = (OSError, serial.SerialException)
else:
    PORT_ERRORS = (OSError, termios.error)

IOSSIOSPEED = 0x80045402
SERIAL_BAUD = 2_000_000
CH340_VENDOR_ID = 0x1A86  # QinHeng Electronics — this adapter's USB-UART chip

BITRATE_CODES = {1000000: 0x01, 800000: 0x02, 500000: 0x03, 400000: 0x04,
                  250000: 0x05, 200000: 0x06, 125000: 0x07, 100000: 0x08,
                  50000: 0x09, 20000: 0x0A, 10000: 0x0B, 5000: 0x0C}

VARLEN_PROTOCOL = 0x12
FRAME_TYPE_STD = 0x01
MODE_NORMAL = 0x00


def now():
    return time.time()


def find_port():
    """Locate the Waveshare adapter's serial port. It enumerates as a CH340
    USB-UART bridge (/dev/cu.usbserial-* on macOS) — the same node pattern
    as the K+DCAN K-line cable (FTDI), so we disambiguate by USB vendor ID
    via ioreg rather than by path alone."""
    if IS_WINDOWS:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid == CH340_VENDOR_ID:
                return p.device
        return None
    try:
        out = subprocess.run(["ioreg", "-l", "-w0"], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    last_vendor = None
    for line in out.splitlines():
        m = re.search(r'"idVendor"\s*=\s*(\d+)', line)
        if m:
            last_vendor = int(m.group(1))
            continue
        m = re.search(r'"IOCalloutDevice"\s*=\s*"([^"]+)"', line)
        if m and last_vendor == CH340_VENDOR_ID:
            return m.group(1)
    # Fallback if ioreg parsing didn't turn up anything (unlikely) — same
    # glob can_transport.py uses for the K-line cable, so only correct when
    # that cable isn't also plugged in.
    cands = glob.glob("/dev/cu.usbserial*")
    return cands[0] if cands else None


class _PosixSerial:
    def __init__(self, port):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = termios.B9600  # nominal only; real speed set below
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        fcntl.ioctl(self.fd, IOSSIOSPEED, struct.pack("Q", SERIAL_BAUD))

    def wait_readable(self, timeout):
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        return bool(r)

    def read(self, n):
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return b""

    def write(self, data):
        os.write(self.fd, data)

    def flush_input(self):
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self):
        os.close(self.fd)


class _WindowsSerial:
    def __init__(self, port):
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = SERIAL_BAUD
        self.ser.timeout = 0
        self.ser.write_timeout = 2
        self.ser.open()

    def wait_readable(self, timeout):
        end = time.time() + max(0.0, timeout)
        while True:
            if self.ser.in_waiting:
                return True
            if time.time() >= end:
                return self.ser.in_waiting > 0
            time.sleep(0.001)

    def read(self, n):
        return self.ser.read(n)

    def write(self, data):
        self.ser.write(data)

    def flush_input(self):
        self.ser.reset_input_buffer()

    def close(self):
        self.ser.close()


def _make_io(port):
    return _WindowsSerial(port) if IS_WINDOWS else _PosixSerial(port)


class WaveshareCanError(Exception):
    pass


class WaveshareCanPort:
    """Same public surface as can_transport.SlcanPort: send_frame,
    recv_frame, drain_rx, reopen, close, plus .port/.show_raw for callers
    that inspect them."""

    def __init__(self, port=None, bitrate=500000, show_raw=False,
                 rawlog_path="can_raw.log"):
        self.port = port or find_port()
        if not self.port:
            raise WaveshareCanError(
                "no Waveshare USB-CAN-A found (looked for a CH340 device, "
                "vendor 0x1A86) — is it plugged in?")
        self.bitrate = bitrate
        self.show_raw = show_raw
        self.rawlog = open(rawlog_path, "a") if rawlog_path else None
        self.buf = b""
        self._open(self.port)

    def _open(self, port):
        self.io = _make_io(port)
        self.buf = b""
        self.io.flush_input()
        code = BITRATE_CODES.get(self.bitrate)
        if not code:
            raise WaveshareCanError(f"unsupported bitrate {self.bitrate}")
        cfg = self._build_config(code)
        self.io.write(cfg)
        self._log(">>", "config " + cfg.hex(" "))
        self._drain_startup_burst()

    def _drain_startup_burst(self, budget=0.5):
        """Every (re)config makes the adapter dump a burst of its stray
        background frame (~0.5s worth, confirmed empirically) before
        settling to a low idle rate. Absorb that here so callers with
        normal sub-second timeouts (e.g. obd2.supported_pids's default
        0.6s) aren't racing it for their first real request/response."""
        deadline = now() + budget
        while now() < deadline:
            self._pump(deadline - now())
            self.buf = b""

    @staticmethod
    def _build_config(can_baud_code):
        body = bytearray(20)
        body[0] = 0xAA
        body[1] = 0x55
        body[2] = VARLEN_PROTOCOL
        body[3] = can_baud_code
        body[4] = FRAME_TYPE_STD
        # filter/mask (bytes 5-12) left 0x00 = accept everything, confirmed
        # empirically against the real car
        body[13] = MODE_NORMAL
        body[14] = 0x00  # auto-retransmit enabled
        body[19] = sum(body[2:19]) & 0xFF
        return bytes(body)

    def reopen(self, wait=None):
        try:
            self.io.close()
        except PORT_ERRORS:
            pass
        deadline = None if wait is None else now() + wait
        while deadline is None or now() < deadline:
            cand = find_port()
            if cand:
                try:
                    self._open(cand)
                    self.port = cand
                    return True
                except (PORT_ERRORS, WaveshareCanError):
                    pass
            time.sleep(0.5)
        return False

    def close(self):
        if self.rawlog:
            self.rawlog.close()
        self.io.close()

    def _log(self, dirn, text):
        if self.show_raw:
            print(f"    {dirn} {text}")
        if self.rawlog:
            self.rawlog.write(f"{dirn} {text}\n")
            self.rawlog.flush()

    def _pump(self, wait):
        if self.io.wait_readable(wait):
            chunk = self.io.read(4096)
            if chunk:
                self.buf += chunk

    def _try_parse_frame(self):
        while True:
            if len(self.buf) < 2:
                return None
            if self.buf[0] != 0xAA:
                self.buf = self.buf[1:]
                continue
            type_byte = self.buf[1]
            ext = bool(type_byte & 0x20)
            dlc = type_byte & 0x0F
            idlen = 4 if ext else 2
            frame_len = 2 + idlen + dlc + 1
            if len(self.buf) < frame_len:
                return None  # wait for the rest to arrive
            id_bytes = self.buf[2:2 + idlen]
            data = self.buf[2 + idlen:2 + idlen + dlc]
            trailer = self.buf[2 + idlen + dlc]
            if trailer != 0x55:
                self.buf = self.buf[1:]  # resync
                continue
            mask = 0x1FFFFFFF if ext else 0x7FF
            can_id = int.from_bytes(id_bytes, "little") & mask
            raw = self.buf[:frame_len]
            self.buf = self.buf[frame_len:]
            self._log("<<", raw.hex(" "))
            return (can_id, bytes(data), ext)

    def send_frame(self, can_id, data, extended=False):
        if len(data) > 8:
            raise WaveshareCanError("CAN data frame max 8 bytes")
        type_byte = 0xC0 | (0x20 if extended else 0x00) | (len(data) & 0x0F)
        id_bytes = struct.pack("<I" if extended else "<H",
                                can_id & (0x1FFFFFFF if extended else 0x7FF))
        frame = bytes([0xAA, type_byte]) + id_bytes + bytes(data) + b"\x55"
        self.io.write(frame)
        self._log(">>", frame.hex(" "))

    def recv_frame(self, timeout):
        deadline = now() + timeout
        while True:
            f = self._try_parse_frame()
            if f:
                return f
            remaining = deadline - now()
            if remaining <= 0:
                return None
            self._pump(min(0.02, remaining))

    def drain_rx(self):
        while self._try_parse_frame():
            pass
