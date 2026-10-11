#!/usr/bin/env python
"""A stand-in for the eflesh_cmd_stream firmware on a pseudo-terminal.

`FirmwareSim().port` is a real tty path, so `EFleshDevice` and the tuner run
their actual serial code against it -- framing, resync, STOP/START handshakes
and all. It speaks the same command protocol as the sketch and mirrors its
settings table, so keep the two in step when the firmware changes.

The data is synthetic: per-chip offsets, noise that shrinks with OSR and the
digital filter, quantised to the current uT/LSB, and a slow press on the middle
chip of each board. Failed reads are simulated when the frame period is shorter
than the conversion time, as on the real chip.

POSIX only (uses pty).
"""

import os
import pty
import select
import struct
import threading
import time
import tty

import numpy as np

MAGS_PER_BOARD = 5

# name, default, lo, hi, description -- mirrors `params[]` in eflesh_cmd_stream.ino
PARAMS = [
    ("gain", 1, 0, 7, "Analog gain GAIN_SEL: 0 = lowest gain (widest range), 7 = highest"),
    ("res_x", 2, 0, 3, "X output resolution RES_X: each step doubles uT/LSB and range"),
    ("res_y", 2, 0, 3, "Y output resolution RES_Y: each step doubles uT/LSB and range"),
    ("res_z", 2, 0, 3, "Z output resolution RES_Z: each step doubles uT/LSB and range"),
    ("filt", 4, 0, 7, "Digital filter DIG_FILT: higher = less noise, slower conversion"),
    ("osr", 0, 0, 3, "Magnetic oversampling OSR: higher = less noise, slower conversion"),
    ("osr2", 0, 0, 3, "Temperature oversampling OSR2: higher = less noise, slower conversion"),
    ("tcmp", 0, 0, 1, "On-chip temperature compensation TCMP_EN"),
    ("period_us", 500, 0, 100000, "Delay between frames, microseconds"),
    ("ascii", 0, 0, 1, "Output: 0 = binary frames (anyskin), 1 = tab-separated text (Serial Plotter)"),
]
CHIP_PARAMS = {"gain", "res_x", "res_y", "res_z", "filt", "osr", "osr2", "tcmp"}
GAIN_MULT = [5.0, 4.0, 3.0, 2.5, 2.0, 1.66666667, 1.33333333, 1.0]
ADDRS = [0x0C, 0x11, 0x12, 0x13, 0x10]
READ_US_PER_CHIP = 350      # I2C burst read at 400 kHz, roughly as measured


class FirmwareSim:
    def __init__(self, num_boards: int = 2, mux: bool = True, seed: int = 0):
        self.num_boards = num_boards
        self.mux = mux
        self.mags = num_boards * MAGS_PER_BOARD
        self.values = {name: d for name, d, *_ in PARAMS}
        self.streaming = True
        self._rng = np.random.default_rng(seed)
        self._offset = self._rng.normal(0, 1500, (self.mags, 3))
        self._held = np.zeros((self.mags, 4), np.float32)
        self._fails = [0] * self.mags
        self._frames = 0
        self._since = time.monotonic()
        self._t0 = time.monotonic()

        self._master, slave = pty.openpty()
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self._slave = slave
        os.set_blocking(self._master, False)
        self._out = bytearray()
        self._inbuf = b""
        self._alive = True
        self._say("Mux at 0x70" if mux else "No mux, reading one board on the main bus")
        self._say(f"Streaming {self.mags} magnetometers, {self.frame_bytes} bytes/frame. "
                  "Send HELP for commands.")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def frame_bytes(self) -> int:
        return self.mags * 16 + 2

    def close(self) -> None:
        self._alive = False
        self._thread.join(timeout=1)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    # ------------------------------------------------------------ derived values

    def lsb(self, axis: int) -> float:
        base = 0.242 if axis == 2 else 0.150
        res = self.values[("res_x", "res_y", "res_z")[axis]]
        return base * GAIN_MULT[self.values["gain"]] * (1 << res)

    def range_mt(self, axis: int) -> float:
        res = self.values[("res_x", "res_y", "res_z")[axis]]
        half = res == 3 and not self.values["tcmp"]
        return (16384 if half else 32768) * self.lsb(axis) / 1000

    def conv_us(self) -> int:
        v = self.values
        per_axis = 67 + 64 * (1 << v["osr"]) * (2 + (1 << v["filt"]))
        return 3 * per_axis + 67 + 192 * (1 << v["osr2"])

    # ------------------------------------------------------------ I/O loop

    def _say(self, line: str) -> None:
        self._out += line.encode() + b"\r\n"

    def _run(self) -> None:
        next_frame = time.monotonic()
        while self._alive:
            self._flush()
            r, _, _ = select.select([self._master], [], [], 0.0005)
            if r:
                try:
                    self._inbuf += os.read(self._master, 1024)
                except OSError:
                    pass
                while b"\n" in self._inbuf:
                    line, self._inbuf = self._inbuf.split(b"\n", 1)
                    self._handle(line.decode(errors="replace").strip())
            now = time.monotonic()
            # Only queue a frame once the last one has gone out: USB back-pressure.
            if self.streaming and not self._out and now >= next_frame:
                self._emit_frame()
                interval = (self.values["period_us"] + READ_US_PER_CHIP * self.mags) / 1e6
                next_frame = now + interval

    def _flush(self) -> None:
        if not self._out:
            return
        try:
            n = os.write(self._master, self._out)
            del self._out[:n]
        except (BlockingIOError, OSError):
            pass

    def _emit_frame(self) -> None:
        t = time.monotonic() - self._t0
        interval_us = self.values["period_us"] + READ_US_PER_CHIP * self.mags
        late = self.conv_us() > interval_us
        noise_ut = 6.0 / np.sqrt((1 << self.values["osr"]) * (1 + self.values["filt"]))
        press = 400.0 * max(0.0, np.sin(2 * np.pi * 0.3 * t)) ** 2
        for k in range(self.mags):
            if late and self._rng.random() < 0.3:
                self._fails[k] += 1
                continue
            field = self._offset[k] + self._rng.normal(0, noise_ut, 3)
            if k % MAGS_PER_BOARD == 0:
                field[2] -= press
            q = np.array([self.lsb(a) for a in range(3)])
            field = np.round(field / q) * q
            self._held[k] = (25.0 + self._rng.normal(0, 0.3), *field)
        if self.values["ascii"]:
            row = []
            for t_c, x, y, z in self._held:
                row += [f"{x:.2f}", f"{y:.2f}", f"{z:.2f}", f"{t_c:.2f}"]
            self._say("\t".join(row))
        else:
            self._out += struct.pack(f"<{self.mags * 4}f", *self._held.ravel()) + b"\r\n"
        self._frames += 1

    # ------------------------------------------------------------ commands

    def _state(self, tag: str) -> str:
        parts = [f"{k}={v}" for k, v in self.values.items()]
        for a, ax in enumerate("xyz"):
            parts.append(f"lsb_{ax}={self.lsb(a):.4f}")
        for a, ax in enumerate("xyz"):
            parts.append(f"range_{ax}={self.range_mt(a):.2f}")
        parts.append(f"conv_us={self.conv_us()}")
        return f"OK {tag} " + " ".join(parts)

    def _commit(self, staged: dict, tag: str) -> None:
        f, o = staged["filt"], staged["osr"]
        if (f == 0 and o <= 1) or (f == 1 and o == 0):
            self._say(f"ERR filt={f} with osr={o} is not permitted by the MLX90393")
            return
        self.values = staged
        self._say(self._state(tag))

    def _handle(self, line: str) -> None:
        if not line:
            return
        cmd, _, rest = line.partition(" ")
        cmd = cmd.upper()
        if cmd == "STOP":
            self.streaming = False
            self._say("#STOPPED")
            return
        if self.streaming and not self.values["ascii"]:
            self.streaming = False
            self._say("#STOPPED")

        if cmd == "START":
            self._say("OK START")
            if self.values["ascii"]:
                self._say("\t".join(f"{a}{k}" for k in range(self.mags) for a in "xyzt"))
            self.streaming = True
        elif cmd == "INFO":
            boards = " ".join(
                f"b{b}={'ch' + str(b) if self.mux else 'direct'}:"
                + ",".join(f"{a:02X}" for a in ADDRS)
                for b in range(self.num_boards))
            self._say(f"OK INFO fw=eflesh_cmd/1-sim mux={'0x70' if self.mux else 'none'} "
                      f"boards={self.num_boards} mags={self.mags} "
                      f"frame_bytes={self.frame_bytes} {boards}")
        elif cmd == "PARAMS":
            for name, default, lo, hi, desc in PARAMS:
                self._say(f"P {name} {self.values[name]} {default} {lo} {hi} {desc}")
            self._say(f"OK PARAMS {len(PARAMS)}")
        elif cmd == "GET":
            self._say(self._state("GET"))
        elif cmd == "DEFAULTS":
            self._commit({name: d for name, d, *_ in PARAMS}, "DEFAULTS")
        elif cmd == "STATS":
            now = time.monotonic()
            ms = int((now - self._since) * 1000)
            self._say(f"OK STATS frames={self._frames} ms={ms} "
                      f"fails={','.join(map(str, self._fails))}")
            self._frames, self._fails, self._since = 0, [0] * self.mags, now
        elif cmd == "HELP":
            self._say("# commands: STOP START INFO PARAMS GET SET k=v.. DEFAULTS STATS HELP")
            self._say("OK HELP")
        elif cmd == "SET":
            self._set(rest)
        else:
            self._say(f"ERR unknown command {cmd}")

    def _set(self, rest: str) -> None:
        limits = {name: (lo, hi) for name, _, lo, hi, _ in PARAMS}
        staged = dict(self.values)
        pairs = rest.split()
        if not pairs:
            self._say("ERR SET needs at least one k=v")
            return
        for tok in pairs:
            key, sep, val = tok.partition("=")
            key = key.lower()
            if not sep:
                self._say(f"ERR expected k=v, got {tok}")
                return
            if key not in limits:
                self._say(f"ERR unknown setting {key}")
                return
            try:
                v = int(val, 0)
            except ValueError:
                self._say(f"ERR bad number for {key}")
                return
            lo, hi = limits[key]
            if not lo <= v <= hi:
                self._say(f"ERR {key} must be {lo}..{hi}")
                return
            staged[key] = v
        self._commit(staged, "SET")
