# eFlesh firmware for the ESP32-C3

Port of [`../5X_eflesh_stream`](../5X_eflesh_stream) and [`../i2c_scan`](../i2c_scan)
to the ESP32-C3 (developed against the **ESP32-C3 SuperMini**). The upstream
sketches target the Adafruit Trinket M0 and QT Py and do **not** compile on an
ESP32 as written.

| Sketch | Purpose |
| --- | --- |
| `i2c_scan_c3/` | Bus check + address-variant identification. **Run this first.** |
| `5X_eflesh_stream_c3/` | Binary stream with runtime address auto-detection. Use with the Python library. |
| `5X_eflesh_ascii_c3/` | Tab-separated ASCII stream. Human-readable / Serial Plotter. |

## Wiring

eFlesh `J1` is a JST SH 4-pin (Qwiic / STEMMA QT) connector. The SuperMini has no
Qwiic jack, so this is hand-wired — a Qwiic-to-jumper cable (Adafruit 4209) is the
tidiest option.

| J1 | Signal | Qwiic wire | SuperMini |
| --- | --- | --- | --- |
| 1 | GND | black | `GND` |
| 2 | VCC | red | **`3V3`** — never the `5V` pin |
| 3 | I2C | blue | `GPIO8` (`SDA_PIN`) |
| 4 | I2C | yellow | `GPIO9` (`SCL_PIN`) |

`GPIO8`/`GPIO9` are the pins printed as SDA/SCL on the SuperMini pinout card and
are the ESP32 core's default I2C pair for the C3. (The board schematic instead
labels `GPIO4`/`GPIO5` — on the C3 the GPIO matrix makes this purely a software
choice, so both work; `GPIO8`/`GPIO9` is the combination proven in practice.)

Notes:

- `GPIO8` also drives the blue LED. It will flicker with bus traffic. Cosmetic only.
- `GPIO9` is the BOOT strapping pin, with a 10 k pull-up and the BOOT button to
  ground. At runtime that pull-up sits in parallel with the sensor board's and
  slightly improves rise time. The one thing to avoid: holding BOOT across a
  reset drags SCL low and brings the chip up in download mode instead of running
  the sketch. If the board comes up mute after a reset, check that before
  suspecting firmware.
- The eFlesh board carries the main I2C pull-ups — do not add any more. Keep the
  cable short (<= 15 cm) for 400 kHz.
- MLX90393 is **not** 5 V tolerant and there is no level shifter on the board.

## Arduino IDE setup

1. `git submodule update --init` in the repo root, then move
   `arduino/arduino-MLX90393` into your Arduino `libraries/` folder. Without this
   `<MLX90393.h>` will not resolve.
2. Board: **ESP32C3 Dev Module**.
3. **USB CDC On Boot: Enabled.** Without it `Serial` maps to UART0 on
   `GPIO20`/`GPIO21`, which go nowhere, and you get no output.

## Bring-up

1. Upload `i2c_scan_c3`. Expect one of the two known variants:

   ```
   0x0C 0x0D 0x0E 0x0F 0x10    -> "all consecutive"
   0x0C 0x10 0x11 0x12 0x13    -> "white set"
   ```

   If nothing is found, swap `SDA_PIN` and `SCL_PIN` and re-upload — no rewiring
   needed. Some-but-not-all-five points at solder joints, not the bus.
2. Upload `5X_eflesh_stream_c3`. On boot it prints a short ASCII banner naming
   the addresses it found and the order it will stream them in, then switches to
   the binary stream. The Python parser resyncs on `\r\n`, so the banner costs a
   couple of frames and nothing more.
3. Read it from Python at `/dev/ttyACM0`. No baudrate flag needed: the C3 is
   native USB CDC, so the `115200` in the sketch is ignored and costs nothing.
   (On a board with a USB-UART bridge it would be a real ~140 frames/s ceiling —
   not the case here.)

## Differences from the upstream (QT Py) sketches

- Removed `#define Serial SERIAL_PORT_USBVIRTUAL` — SAMD-only, and the compile
  error you hit first.
- Replaced `while (!Serial)` with `delay(2000)`.
- `Wire.begin()` -> `Wire.begin(SDA_PIN, SCL_PIN)`.
- **Three-stage bring-up before the address scan.** This is the important one, and it
  is not a portability fix -- it is a bug the QT Py sketch has too, just less visibly.
  The magnetometers are powered from 3V3 and do **not** reset when the C3 does, and the
  C3 resets every time a host opens the serial port (DTR). So the chips come back still
  in burst mode from the previous run, and a chip caught mid-transfer keeps holding SDA
  low. Either way the address scan finds *nothing*, and the sketch streams zeros
  forever. The only cure from outside is physically unplugging the Qwiic connector,
  which cuts their power. Observed symptom: `i2c_scan` reports 0 devices on a board
  whose sensors stream perfectly a moment later, and only a replug fixes it. So setup()
  now does, in order:
    1. `recoverI2CBus()` -- nine SCL pulses plus a STOP, freeing a slave that is holding
       SDA low. Reports whether the line was actually stuck.
    2. `quiesceKnownAddresses()` -- `EX` then `RT` to `0x0C..0x13`, taking every chip out
       of burst mode and resetting it. This is the replug, in software.
    3. The scan, at 100kHz, raised to 400kHz once the chips have answered.
- **Falls back to the white set** if the scan still comes up short, initializing
  `0x0C, 0x11, 0x12, 0x13, 0x10` directly the way the ReSkin sketch does, rather than
  streaming zeros.
- `loop()` reads only the sensors that were actually initialized. This is a real
  bug fix, not just a port: `MLX90393`'s constructor leaves its `TwoWire*`
  member uninitialized, and the upstream `loop()` calls `readBurstData()` on all
  five indices regardless of how many `begin()` succeeded. With fewer than five
  chips on the bus, SAMD usually limps along on the garbage pointer; the C3
  panics and reboot-loops. The port zeroes every buffer in `setup()` and tracks
  `activeCount`, so missing chips stream zeros instead of crashing and the
  record length stays fixed.

Everything else — the I2C scan, the two known address sets and their stream
orders, the gain / resolution / digital-filter configuration, and the wire format
(5 chips x 4 floats x 4 bytes + `\r\n` = 82 bytes/frame) — is untouched. Both the
SAMD21 and the C3 are little-endian 32-bit with 4-byte IEEE-754 floats, so
`MLX90393::txyz` is 16 bytes with no padding on either and the raw struct dump
stays compatible.

## Known gotcha: reset on port open

ESP32 boards reset when a host asserts DTR/RTS, so opening the port re-runs
`setup()` — including the full I2C scan and the banner — and the ROM bootloader's
log lands in the stream as garbage. The parser resyncs on `\r\n`
([`sensor.py:137`](https://github.com/raunaqbhirangi/reskin_sensor/blob/main/reskin_sensor/sensor.py#L137)), so you lose
a few frames and recover. But the wait loop at
[`sensor.py:153`](https://github.com/raunaqbhirangi/reskin_sensor/blob/main/reskin_sensor/sensor.py#L153) has no
timeout, so while the board is booting `get_sample()` spins rather than erroring.
If that bothers you, pass `dtr=False` or sleep ~2 s after opening the port. Note
this port's boot is slower than the ReSkin one — the address scan walks
0x08..0x77 before streaming starts.
