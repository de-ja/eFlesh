#!/usr/bin/env python
"""Host driver for the eflesh_cmd_stream firmware.

The firmware owns the list of settings: `PARAMS` returns each one's name,
value, default, range and description, and `GET` returns the current values
plus derived quantities (uT per LSB, output range, conversion time). This
driver passes all of that through rather than keeping its own copy, so a
setting added to the firmware shows up here and in the tuner without a Python
change.

Unlike `stream.EFleshSource`, which delegates to anyskin, this talks to the
port directly: anyskin only reads, and retuning needs to write. The binary
frame format is the same one described in PROTOCOL.md.

    dev = EFleshDevice("/dev/ttyACM0")
    dev.open()                      # stops the stream, reads INFO / PARAMS / GET
    dev.set(gain=3, filt=5)         # validated and applied by the firmware
    dev.start()                     # binary frames into a ring buffer
    t, data = dev.snapshot(500)     # data: (n, mags, 4) as t, x, y, z
    dev.close()                     # leaves the board streaming binary
"""

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

BAUDRATE = 115200
STOPPED = b"#STOPPED\r\n"
STARTED = b"OK START\r\n"

#: Settings the driver manages itself. `ascii` selects the wire format, and
#: the driver needs binary frames while it is streaming.
DRIVER_OWNED = frozenset({"ascii"})

Value = Union[int, float, str]


class DeviceError(RuntimeError):
    """The firmware rejected a command, or did not answer like eflesh_cmd_stream."""


@dataclass
class Param:
    """One firmware setting, as described by `PARAMS`."""

    name: str
    value: int
    default: int
    lo: int
    hi: int
    desc: str


@dataclass
class Board:
    name: str          # "b0", "b1", ...
    where: str         # "direct" or "ch<N>" on the mux
    addrs: List[int]   # stream order; 0 is an empty slot


@dataclass
class DeviceInfo:
    fw: str
    mux: str
    mags: int
    frame_bytes: int
    boards: List[Board]


def _parse_value(text: str) -> Value:
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _parse_pairs(tokens: List[str]) -> Dict[str, Value]:
    out = {}
    for tok in tokens:
        key, sep, val = tok.partition("=")
        if sep:
            out[key] = _parse_value(val)
    return out


def _parse_info(reply: str) -> DeviceInfo:
    tokens = reply.split()[2:]
    kv = dict(t.partition("=")[::2] for t in tokens if "=" in t)
    boards = []
    for key in sorted(k for k in kv if k[0] == "b" and k[1:].isdigit()):
        where, _, addrs = kv[key].partition(":")
        boards.append(Board(key, where, [int(a, 16) for a in addrs.split(",") if a]))
    return DeviceInfo(
        fw=kv.get("fw", "?"),
        mux=kv.get("mux", "?"),
        mags=int(kv.get("mags", 0)),
        frame_bytes=int(kv.get("frame_bytes", 2)),
        boards=boards,
    )


class EFleshDevice:
    """Configure and stream an eFlesh board running eflesh_cmd_stream.

    Every command runs with the stream stopped: the firmware never answers in
    the middle of a binary frame, and this class pauses and resumes the reader
    around each command so callers do not have to.
    """

    def __init__(self, port: str, baudrate: int = BAUDRATE, buffer: int = 8192,
                 timeout: float = 1.5):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._buffer = buffer
        self._ser = None
        self._io = threading.RLock()        # serialises commands
        self._buf_lock = threading.Lock()   # guards the ring buffer
        self._reader: Optional[threading.Thread] = None
        self._halt = threading.Event()

        self.info: Optional[DeviceInfo] = None
        self.params: List[Param] = []
        self.state: Dict[str, Value] = {}

        self._t = np.zeros(0)
        self._data = np.zeros((0, 0, 4), dtype=np.float32)
        self._count = 0       # frames written since the buffer was sized
        self.frames = 0       # good frames since open()
        self.resyncs = 0      # frames dropped while re-finding the boundary

    # ------------------------------------------------------------ lifecycle

    def open(self) -> None:
        import serial

        self._ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        self._stop_device()
        self.info = _parse_info(self.command("INFO"))
        self.params = self._read_params()
        self.state = self._parse_state(self.command("GET"))
        if self.state.get("ascii"):
            self.set(ascii=0)
        self._size_buffer()

    def close(self, leave_ascii: bool = False) -> None:
        """Release the port, leaving the board streaming.

        By default it streams binary, as at power-up, so anyskin and
        EFleshSource can attach. `leave_ascii=True` leaves tab-separated text
        for the Arduino Serial Plotter instead.
        """
        if self._ser is None:
            return
        try:
            self.stop()
            if leave_ascii:
                self.command("SET ascii=1")
            self._ser.write(b"START\n")
            self._ser.flush()
        finally:
            self._ser.close()
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    @property
    def streaming(self) -> bool:
        return self._reader is not None

    # ------------------------------------------------------------ streaming

    def start(self) -> None:
        """Start binary streaming into the ring buffer."""
        with self._io:
            if self._reader is not None:
                return
            self._ser.reset_input_buffer()
            self._ser.write(b"START\n")
            got = self._ser.read_until(STARTED)
            if not got.endswith(STARTED):
                raise DeviceError(f"START not acknowledged; got {got[-80:]!r}")
            self._halt.clear()
            self._ser.timeout = 0.1        # so stop() is not held up by a silent port
            self._reader = threading.Thread(target=self._read_frames, daemon=True)
            self._reader.start()

    def stop(self) -> None:
        """Stop streaming. The buffer keeps what was already received."""
        with self._io:
            if self._reader is not None:
                self._halt.set()
                self._reader.join()
                self._reader = None
                self._ser.timeout = self.timeout
            self._stop_device()

    def snapshot(self, n: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]:
        """The newest `n` frames, oldest first.

        Returns (t, data): host receive times in seconds, and (n, mags, 4)
        float32 as temperature (C), x, y, z (uT) per magnetometer.
        """
        with self._buf_lock:
            have = min(self._count, len(self._t))
            n = have if n is None else min(n, have)
            if n == 0:
                return np.zeros(0), np.zeros((0,) + self._data.shape[1:], np.float32)
            end = self._count % len(self._t)
            idx = (np.arange(end - n, end)) % len(self._t)
            return self._t[idx].copy(), self._data[idx].copy()

    def clear(self) -> None:
        with self._buf_lock:
            self._count = 0

    def _size_buffer(self) -> None:
        mags = self.info.mags if self.info else 0
        with self._buf_lock:
            self._t = np.zeros(self._buffer)
            self._data = np.zeros((self._buffer, mags, 4), dtype=np.float32)
            self._count = 0

    def _read_frames(self) -> None:
        ser = self._ser
        width = self.info.frame_bytes
        mags = self.info.mags
        while not self._halt.is_set():
            buf = ser.read(width)
            if len(buf) < width:
                continue                   # timeout; check the halt flag again
            if buf[-2:] != b"\r\n":
                # The float payload can itself contain \r\n, so never split on
                # it. Discard to the next boundary and take fixed lengths again.
                ser.read_until(b"\r\n")
                self.resyncs += 1
                continue
            frame = np.frombuffer(buf, dtype="<f4", count=mags * 4).reshape(mags, 4)
            now = time.time()
            with self._buf_lock:
                i = self._count % len(self._t)
                self._t[i] = now
                self._data[i] = frame
                self._count += 1
            self.frames += 1

    # ------------------------------------------------------------ commands

    def command(self, line: str) -> str:
        """Send one command and return its final reply line ("OK ...").

        Pauses the stream around the command if it was running. Raises
        DeviceError on an "ERR" reply.
        """
        with self._io:
            was_streaming = self._reader is not None
            if was_streaming:
                self.stop()
            try:
                return self._exchange(line)[-1]
            finally:
                if was_streaming:
                    self.start()

    def set(self, **values: int) -> Dict[str, Value]:
        """Apply settings atomically. The firmware validates the whole set and
        applies nothing if any value is rejected."""
        if not values:
            return self.state
        pairs = " ".join(f"{k}={int(v)}" for k, v in values.items())
        self.state = self._parse_state(self.command(f"SET {pairs}"))
        self._sync_param_values()
        return self.state

    def defaults(self) -> Dict[str, Value]:
        self.state = self._parse_state(self.command("DEFAULTS"))
        if self.state.get("ascii"):
            self.set(ascii=0)
        self._sync_param_values()
        return self.state

    def stats(self) -> Dict[str, Value]:
        """Frames sent and failed reads per magnetometer since the last call.

        `fails` is a list in stream order. A read fails when a chip does not
        answer in time, typically because the frame period is shorter than its
        conversion time.
        """
        reply = self.command("STATS")
        out = _parse_pairs(reply.split()[2:])
        fails = str(out.get("fails", ""))
        out["fails"] = [int(v) for v in fails.split(",") if v != ""]
        return out

    def tunable(self) -> List[Param]:
        """Settings meant for a user to change: everything but DRIVER_OWNED."""
        return [p for p in self.params if p.name not in DRIVER_OWNED]

    def derived(self) -> Dict[str, Value]:
        """Values the firmware computes from the settings (lsb_*, range_*, ...)."""
        names = {p.name for p in self.params}
        return {k: v for k, v in self.state.items() if k not in names}

    # ------------------------------------------------------------ internals

    def _stop_device(self) -> None:
        self._ser.write(b"STOP\n")
        got = self._ser.read_until(STOPPED)
        if not got.endswith(STOPPED):
            raise DeviceError(
                f"No reply to STOP on {self.port}. Is eflesh_cmd_stream flashed? "
                "The older stream sketches do not accept commands."
            )

    def _exchange(self, line: str) -> List[str]:
        """Write a command; return the reply lines up to and including OK/ERR."""
        self._ser.write(line.encode("ascii") + b"\n")
        lines = []
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            text = raw.decode("ascii", errors="replace").strip()
            if not text or text.startswith("#"):
                continue
            lines.append(text)
            if text.startswith("ERR"):
                raise DeviceError(f"{line!r}: {text[4:]}")
            if text.startswith("OK"):
                return lines
        raise DeviceError(f"No reply to {line!r} within {self.timeout} s")

    def _read_params(self) -> List[Param]:
        params = []
        for text in self._exchange("PARAMS"):
            if not text.startswith("P "):
                continue
            parts = text.split(" ", 6)
            name, value, default, lo, hi = parts[1], *map(int, parts[2:6])
            params.append(Param(name, value, default, lo, hi,
                                parts[6] if len(parts) > 6 else ""))
        return params

    def _parse_state(self, reply: str) -> Dict[str, Value]:
        return _parse_pairs(reply.split()[2:])

    def _sync_param_values(self) -> None:
        for p in self.params:
            if p.name in self.state:
                p.value = int(self.state[p.name])
