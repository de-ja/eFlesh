# eFlesh wire protocol and driver notes

Everything a host-side driver needs to read this sensor. Written for
integrating eFlesh as one driver behind a generic sensor interface.

## Transport

| | |
|---|---|
| Link | USB CDC (native USB on the microcontroller) |
| Device | `/dev/ttyACM*` on Linux, `/dev/cu.usbmodem*` on macOS, `COM*` on Windows |
| Baud | 115200 nominal -- USB CDC ignores it, but pyserial wants a value |
| Flow control | none |
| Microcontroller | Adafruit QT Py M0 (SAMD21) |

**The QT Py does not reset when the port is opened.** Only a 1200-baud touch
triggers the bootloader, so a driver may attach to an already-running stream.

## Frame format

Fixed-length binary records, one per sample:

```
[ mag_0 ][ mag_1 ] ... [ mag_N-1 ][ 0x0D 0x0A ]
```

Each `mag_i` is four little-endian IEEE-754 `float32`, in this order:

| offset | field | unit |
|---|---|---|
| +0 | temperature | degrees C |
| +4 | Bx | microtesla |
| +8 | By | microtesla |
| +12 | Bz | microtesla |

So `frame_length = 16 * num_mags + 2`.

| configuration | num_mags | frame | rate at default settings |
|---|---|---|---|
| one board, direct | 5 | 82 B | 193 Hz, measured |
| two boards via I2C mux | 10 | 162 B | not yet measured; the bus limits it to roughly 180 Hz |

Every frame holds a fresh sample from every chip, so the rate is set by the
chips' conversion time (`conv_us` in `GET`, see below). Older burst-mode
firmware reported ~400 Hz and ~194 Hz, but many of those frames repeated the
previous sample.

### Frame width is decided by the firmware at boot

The mux firmware sizes its frame from however many boards it found when it
started. Two boards give 162 bytes; if one fails to enumerate you silently get
82 instead. A host hardcoded to the larger size then never sees a valid frame
and **sits in its resync loop forever rather than raising** -- a hang, not an
error.

Detect the width instead of assuming it: sync to a `\r\n`, measure the next
handful of `read_until` lengths, take the mode, and derive
`num_mags = (width - 2) // 16`. Reject a width where `(width - 2) % 16 != 0`.
`visualizer/viz_gripper.py:detect_num_mags()` is a working ~20-line version.

### Framing and resync -- do not use readline()

The `\r\n` terminator is **not** a safe delimiter on its own: the float payload
is arbitrary binary and can contain the bytes `0x0D 0x0A`. Read a fixed length
and validate instead:

```python
buf = ser.read(frame_length)
if buf[-2:] != b"\r\n":
    ser.read_until(b"\r\n")   # discard to the next boundary and retry
    continue
values = struct.unpack(f"<{4*num_mags}f", buf[:-2])
```

This also handles startup cleanly. The firmware prints an ASCII banner (mux
address, board channels, addresses, status per chip) before the binary stream
begins, and the resync path walks past it within a frame or two.

## Channel layout

Within one board, magnetometers stream in **physical position order**, not
address order:

| index | position | address (white variant) |
|---|---|---|
| 0 | middle | `0x0C` |
| 1 | left | `0x11` |
| 2 | right | `0x12` |
| 3 | top | `0x13` |
| 4 | bottom | `0x10` |

With two boards behind a mux, boards appear in **ascending mux channel order**:
indices 0-4 are the board on the lowest populated channel, 5-9 the next. The
boot banner prints which channel each board came from.

## Interpreting values

- **Raw values are absolute field**, dominated by a large DC offset from the
  cuboid's own magnets: roughly 1-2 mT for an alternating-polarity build,
  12-15 mT for a same-polarity build. Everything downstream works on dB
  against a baseline, never on raw.
- **Quantisation** is 2.4 uT per LSB on XY and 3.87 uT on Z, at the firmware's
  `setGainSel(0x1)` and `setResolution(0x2,0x2,0x2)`.
- **Noise floor is ~2.5 uT**, about one LSB, so the sensor is
  quantisation-limited. Measure noise only after ~30 s of settling; a baseline
  taken right after handling the cuboid reads several times too high.
- **Full scale** is +/-78.6 mT (XY) and +/-126.9 mT (Z). Nothing in normal use
  comes close.
- **A failed read shows up as 78640.8 uT**, which is raw `0xFFFF` converted.
  The current firmware suppresses these by holding the last good sample, but a
  host-side sanity check on `abs(value) > 50000` is cheap insurance.

## Baselining on a gripper

Two cuboids mounted on opposing fingers see each other. The neighbour's field
is a large offset that **varies with the gripper gap**, and with same-polarity
builds the two arrays also physically repel, deforming the elastomer and
producing a phantom contact reading that grows as the gripper closes.

Both effects are deterministic functions of gap, so the fix is a
**gap-conditioned baseline**: sweep the gripper through its range with nothing
between the fingers, log all channels against the encoder value, and subtract
the interpolated baseline for the current gap at runtime. A single static
baseline is not sufficient on a gripper.

## Recommended host implementation

Do not reimplement the serial loop. The `anyskin` package already provides it
and is fully parameterised on `num_mags`:

```python
from anyskin import AnySkinProcess

sensor = AnySkinProcess(num_mags=10, port="/dev/ttyACM0", temp_filtered=True)
sensor.start()
sensor.start_streaming()

reading = sensor.last_reading   # [timestamp, 30 floats]
```

It runs the reader in its own process, timestamps each sample, does the
fixed-length-plus-resync framing described above, and with
`temp_filtered=True` masks out the temperature channels, leaving
`3 * num_mags` values ordered `Bx, By, Bz` per magnetometer.

Mapping onto a typical driver interface:

| interface | anyskin |
|---|---|
| `start()` | `start()` then `start_streaming()` |
| `read_latest()` | `last_reading` -> `[t, *values]` |
| `close()` | `pause_streaming()` then `join()` |
| `labels` | `f"{finger}_{position}_{axis}"`, 30 of them |

`anyskin` also ships `AnySkinDummy` for a hardware-free fake.

## Firmware in this repo

| sketch | use |
|---|---|
| `arduino/eflesh_cmd_stream/` | one board direct or several behind a mux, plus live settings over serial (see below) |
| `arduino/mux_scan/` | prints mux address, populated channels, address variant per board |
| `arduino/i2c_scan/` | single-board address variant check |

## Live settings (`eflesh_cmd_stream`)

This sketch boots into the binary stream above, so everything in this document
applies unchanged. It also reads text commands, one per `\n`-terminated line:
`STOP`, `START`, `INFO`, `PARAMS`, `GET`, `SET k=v ...`, `DEFAULTS`, `STATS`,
`HELP`. The full reference is the comment at the top of the sketch.

What a driver needs to know:

- **Stop before you talk.** In binary mode any command stops the stream first
  and prints `#STOPPED`, always after a complete frame. Read until
  `#STOPPED\r\n`, then each command answers with lines ending in `OK ...` or
  `ERR ...`. `START` answers `OK START\r\n`, and the next byte is the start of a
  frame.
- **The firmware owns the settings list.** `PARAMS` gives each setting's name,
  value, default and range; `GET` adds derived values (`lsb_*` in uT/LSB,
  `range_*` in mT, `conv_us`). Build controls from these rather than hardcoding.
- **`SET` is atomic.** All pairs are validated first, including the OSR /
  DIG_FILT pairs the MLX90393 does not permit, and nothing is applied on `ERR`.
- **Settings are not persistent.** They reset to the defaults on power-up.
- **Frame rate follows the settings.** One frame per `conv_us + period_us`, so
  more OSR or filtering means fewer frames: 193 Hz at the defaults, 333 Hz at
  `filt=2`, 18 Hz at `osr=2 filt=6`, 5 Hz at the slowest. The firmware runs
  the chips in single-measurement mode and reads each one only after its
  conversion finishes, so there are no repeated frames and `STATS` fails stay
  at 0.

`eflesh/device.py` (`EFleshDevice`) implements all of this.
