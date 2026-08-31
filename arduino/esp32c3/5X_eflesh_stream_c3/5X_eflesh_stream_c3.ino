/*
  eFlesh Board Code
  By: Venkatesh P
  Date: August 1, 2025
  License: This code is public domain but you buy me a beer if you use this and we meet someday (Beerware license).

  Credits: Adapted from (ReSkin (https://reskin.dev) and AnySkin (https://any-skin.github.io) repos) - maintained by Raunaq Bhirangi, Tess Hellebrekers
  Library: Heavily based on original MLX90393 library from Theodore Yapo (https://github.com/tedyapo/arduino-MLX90393)
  Library: https://github.com/tesshellebrekers/arduino-MLX90393  (required)

  --- ESP32-C3 port of ../../5X_eflesh_stream ---
  Changes from the Trinket M0 / QT Py original:
    - removed  #define Serial SERIAL_PORT_USBVIRTUAL  (SAMD-only, will not compile here)
    - replaced the  while (!Serial)  spin with a fixed delay
    - Wire.begin() now names SDA/SCL explicitly (GPIO8/GPIO9, which also happens
      to be the ESP32 core default for the C3)
    - loop() now reads only the sensors that were actually initialized. On the
      original this was a latent bug: MLX90393's constructor leaves its TwoWire*
      uninitialized, so calling readBurstData() on an index that never got
      begin() dereferences a wild pointer. SAMD usually limps on; the C3 panics
      and reboot-loops. See the activeCount guard below.
  Serial.begin(115200) is unchanged: the C3 enumerates as native USB CDC, so the
  baudrate argument is ignored and throughput is not limited by it.

  The wire format is untouched: 5 chips x 4 floats x 4 bytes + "\r\n" = 82
  bytes per frame. Both SAMD21 and the C3 are little-endian 32-bit with 4-byte
  IEEE-754 floats, so MLX90393::txyz is 16 bytes with no padding on either and
  the raw struct dump stays compatible with the Python library.
*/

#include <Wire.h>
#include <MLX90393.h>

// eFlesh J1 is a JST SH 4-pin (Qwiic/STEMMA QT) connector:
//   pin 1 = GND, pin 2 = VCC (3V3), pins 3 and 4 = the I2C pair.
// Wire pin 3 to SDA_PIN and pin 4 to SCL_PIN. If the chips do not answer, swap
// these two numbers: getting it backwards is harmless - the bus simply stays
// quiet. Run i2c_scan_c3 first to settle this.
//
// GPIO8 also drives the SuperMini's blue LED, so it will flicker with bus
// traffic - cosmetic only. GPIO9 is the BOOT strapping pin: do not hold the
// BOOT button down across a reset while the bus is live, or the chip comes up
// in download mode instead of running this sketch.
#define SDA_PIN 8
#define SCL_PIN 9

// MLX90393 objects and data buffers
static const uint8_t NUM_SENSORS = 5;
MLX90393 mlx[NUM_SENSORS];
MLX90393::txyz dataBuf[NUM_SENSORS];

// How many of mlx[] actually had begin() called. Indices >= activeCount are
// never touched on the bus; their dataBuf entries stay zeroed so the binary
// record keeps its fixed 80-byte length.
uint8_t activeCount = 0;

// Per-chip failed-read counters, and a frame counter used to report them once.
uint16_t readErrors[NUM_SENSORS] = {0};
uint16_t frameCount = 0;

// Known sequences
const uint8_t TARGETS_ALL_CONSEC[NUM_SENSORS] = {0x0C, 0x0D, 0x0E, 0x0F, 0x10};
const uint8_t TARGETS_WHITE_SET[NUM_SENSORS]   = {0x0C, 0x10, 0x11, 0x12, 0x13};

// Forward decls
void recoverI2CBus();
void quiesceKnownAddresses();
void scanI2C(uint8_t* found, uint8_t& count);
void chooseOrderedAddresses(const uint8_t* found, uint8_t count, uint8_t* ordered, uint8_t& orderedCount);
bool hasExactSet(const uint8_t* found, uint8_t count, const uint8_t* pattern);
void sortAscending(uint8_t* arr, uint8_t n);

void setup() {
  // Native USB needs a moment to enumerate before the host can open it. The
  // baudrate argument is ignored by USB CDC.
  Serial.begin(115200);
  delay(2000);

  // Start I2C on the pins wired to J1, set to fast mode (400kHz)
  // Free a slave that is still holding SDA low from an interrupted transfer.
  // This matters here: opening the serial port asserts DTR and reboots the C3,
  // which can land mid-byte. The magnetometers are NOT reset by that -- they
  // stay powered from 3V3 -- so a chip left clocking out a byte keeps the bus
  // wedged across every subsequent C3 reset, and the address scan then finds
  // nothing at all. Only a power cycle or these clock pulses clear it.
  recoverI2CBus();

  Wire.begin(SDA_PIN, SCL_PIN);
  // Scan at 100kHz: address probing is where a marginal bus fails first, and
  // the proven ReSkin scanner runs at this speed. Raised to 400kHz below, once
  // the chips have answered.
  Wire.setClock(100000);
  delay(10);

  // Software stand-in for unplugging the Qwiic connector. The magnetometers
  // keep their state across a C3 reset -- they are still powered -- so they
  // come back still in burst mode from the previous run, and a chip that was
  // mid-transfer when the reset hit stays wedged. Physically replugging works
  // because it cuts 3V3. EX then RT does the same thing over the bus.
  quiesceKnownAddresses();

  // Zero every buffer up front: anything the address scan does not fill stays
  // zero for the life of the run.
  for (uint8_t i = 0; i < NUM_SENSORS; ++i) {
    dataBuf[i] = {0, 0, 0, 0};
  }

  uint8_t found[16] = {0};
  uint8_t foundCount = 0;
  scanI2C(found, foundCount);

  Serial.println(F("I2C scan complete."));
  Serial.print(F("Found MLX candidates: "));
  for (uint8_t i = 0; i < foundCount; ++i) {
    Serial.print("0x"); Serial.print(found[i], HEX); Serial.print(" ");
  }
  Serial.println();

  uint8_t ordered[NUM_SENSORS] = {0};
  uint8_t orderedCount = 0;
  chooseOrderedAddresses(found, foundCount, ordered, orderedCount);

  if (orderedCount != NUM_SENSORS) {
    // The scan came up short. Do NOT stream zeros: the ReSkin sketch drives
    // these same boards with hardcoded addresses and no scan at all, which is
    // proof the chips answer a direct begin() even when probing them does not.
    // Fall back to the white set in eFlesh's channel order and let begin()
    // report per-chip status below.
    Serial.print(F("[WARN] Scan found "));
    Serial.print(orderedCount);
    Serial.println(F(" of 5 target addresses. Falling back to the white set."));
    ordered[0] = 0x0C; ordered[1] = 0x11; ordered[2] = 0x12; ordered[3] = 0x13; ordered[4] = 0x10;
    orderedCount = NUM_SENSORS;
  }

  // Chips answer at 100kHz; run the bus at full speed for streaming.
  Wire.setClock(400000);

  // Initialize sensors in the chosen order
  for (uint8_t i = 0; i < orderedCount; ++i) {
    byte status = mlx[i].begin(ordered[i], -1, Wire);
    Serial.print(F("Init MLX[")); Serial.print(i); Serial.print(F("] @0x"));
    Serial.print(ordered[i], HEX);
    Serial.print(F(" status="));
    Serial.print(status, HEX);
    Serial.println(status ? F("  <-- NOT RESPONDING") : F("  ok"));

    // Configs
    mlx[i].setGainSel(0x1);
    mlx[i].setResolution(0x2, 0x2, 0x2);
    mlx[i].setDigitalFiltering(0x4);

    // Start burst mode (Temp + X + Y + Z) = 0xF
    mlx[i].startBurst(0xF);
  }

  // Only these indices are safe to read in loop(); the rest keep streaming
  // zeros so the binary record stays a fixed length.
  activeCount = orderedCount;

  Serial.print(F("Streaming order: "));
  for (uint8_t i = 0; i < activeCount; ++i) {
    Serial.print("0x"); Serial.print(ordered[i], HEX); Serial.print(" ");
  }
  Serial.println();
}

void loop() {
  // Read in the same order the sensors were initialized. Indices at or past
  // activeCount never had begin() called, so they must not be read - their
  // buffers were zeroed in setup() and stay that way.
  for (uint8_t i = 0; i < activeCount; ++i) {
    // Only commit a sample the chip actually returned. readBurstData() bails
    // out early on a failed I2C read, but the library still runs convertRaw()
    // over an UNINITIALIZED stack struct and hands it back -- so ignoring the
    // status (as the upstream sketches do) publishes stack garbage as if it
    // were a reading. A chip that misses a frame keeps its previous value.
    MLX90393::txyz sample;
    if (mlx[i].readBurstData(sample) == MLX90393::STATUS_OK) {
      dataBuf[i] = sample;
    } else {
      ++readErrors[i];
    }
  }

  // Report a chip that is failing every read, once, rather than streaming a
  // frozen value forever with no indication anything is wrong.
  if (++frameCount == 200) {
    for (uint8_t i = 0; i < activeCount; ++i) {
      if (readErrors[i] > 190) {
        Serial.print(F("[warn] chip ")); Serial.print(i);
        Serial.print(F(" failed ")); Serial.print(readErrors[i]);
        Serial.println(F("/200 reads -- check its solder joints"));
      }
    }
  }

  // Write binary records in-order
  for (uint8_t i = 0; i < NUM_SENSORS; ++i) {
    Serial.write((uint8_t*)&dataBuf[i], sizeof(dataBuf[i]));
  }
  Serial.println();

  // adjust for desired sample rate
  delayMicroseconds(500);
}

// Probe by zero-length write: START, address+W, expect ACK, STOP. This is the
// classic Arduino scanner idiom and what the QT Py sketch uses.
static bool probeWrite(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// Probe by one-byte read. For the MLX90393 this returns its status byte, so it
// is a meaningful transaction rather than an empty one -- and it does not
// depend on how the platform implements a zero-length write.
static bool probeRead(uint8_t addr) {
  bool ok = (Wire.requestFrom(addr, (uint8_t)1) == 1);
  while (Wire.available()) Wire.read();
  return ok;
}

// Scan all I2C addresses to find MLX90393 sensors.
// Tries the write probe first, then the read probe if that found nothing. The
// two disagreeing is itself the diagnosis: the chips are there and answering,
// and only the probe method was wrong.
void scanI2C(uint8_t* found, uint8_t& count) {
  count = 0;
  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    if (probeWrite(addr)) {
      found[count++] = addr;
      if (count >= 16) break;
    }
  }
  if (count > 0) return;

  Serial.println(F("[i2c] write probe found nothing; retrying with read probe"));
  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    if (probeRead(addr)) {
      found[count++] = addr;
      if (count >= 16) break;
    }
  }
}

void chooseOrderedAddresses(const uint8_t* found, uint8_t count, uint8_t* ordered, uint8_t& orderedCount) {
  orderedCount = 0;

  // Copy to a temp for set checks (exact set of 5)
  if (count >= NUM_SENSORS) {
    // Check for the two exact sets
    if (hasExactSet(found, count, TARGETS_ALL_CONSEC)) {
      for (uint8_t i = 0; i < NUM_SENSORS; ++i) ordered[i] = TARGETS_ALL_CONSEC[i];
      orderedCount = NUM_SENSORS;
      return;
    }
    if (hasExactSet(found, count, TARGETS_WHITE_SET)) {
      // custom order: C,11,12,13,10
      ordered[0] = 0x0C; ordered[1] = 0x11; ordered[2] = 0x12; ordered[3] = 0x13; ordered[4] = 0x10;
      orderedCount = NUM_SENSORS;
      return;
    }
  }

  uint8_t tmp[16];
  uint8_t n = count;
  if (n > 16) n = 16;
  for (uint8_t i = 0; i < n; ++i) tmp[i] = found[i];
  sortAscending(tmp, n);

  orderedCount = (n >= NUM_SENSORS) ? NUM_SENSORS : n;
  for (uint8_t i = 0; i < orderedCount; ++i) ordered[i] = tmp[i];
}

bool hasExactSet(const uint8_t* found, uint8_t count, const uint8_t* pattern) {
  uint8_t hits = 0;
  for (uint8_t p = 0; p < NUM_SENSORS; ++p) {
    bool present = false;
    for (uint8_t i = 0; i < count; ++i) {
      if (found[i] == pattern[p]) { present = true; break; }
    }
    if (present) ++hits;
  }
  return (hits == NUM_SENSORS);
}

void sortAscending(uint8_t* arr, uint8_t n) {
  for (uint8_t i = 0; i < n; ++i) {
    for (uint8_t j = i + 1; j < n; ++j) {
      if (arr[j] < arr[i]) {
        uint8_t t = arr[i]; arr[i] = arr[j]; arr[j] = t;
      }
    }
  }
}

// Clock the bus free if a slave is holding SDA low after an interrupted
// transfer. Nine SCL pulses let it finish the byte it was shifting out, then a
// manual STOP releases it. Runs before Wire.begin(), on plain GPIO.
void recoverI2CBus() {
  pinMode(SCL_PIN, OUTPUT_OPEN_DRAIN);
  pinMode(SDA_PIN, INPUT_PULLUP);
  digitalWrite(SCL_PIN, HIGH);
  delayMicroseconds(5);

  uint8_t pulses = 0;
  while (digitalRead(SDA_PIN) == LOW && pulses < 9) {
    digitalWrite(SCL_PIN, LOW);
    delayMicroseconds(5);
    digitalWrite(SCL_PIN, HIGH);
    delayMicroseconds(5);
    ++pulses;
  }

  // STOP condition: SDA released low->high while SCL is high
  pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SDA_PIN, LOW);
  delayMicroseconds(5);
  digitalWrite(SCL_PIN, HIGH);
  delayMicroseconds(5);
  digitalWrite(SDA_PIN, HIGH);
  delayMicroseconds(5);

  pinMode(SDA_PIN, INPUT);
  pinMode(SCL_PIN, INPUT);

  if (pulses) {
    Serial.print(F("[i2c] bus was stuck; released after "));
    Serial.print(pulses);
    Serial.println(F(" clock pulses"));
  }
}

// Send EX (exit mode) then RT (reset) to every address either known board
// variant can use, 0x0C..0x13. Addresses with nothing behind them simply NACK,
// which is harmless. This runs before the scan so the chips are quiet and
// freshly reset by the time they are probed -- otherwise a leftover burst mode
// from the previous run is still driving the bus while we try to enumerate it.
void quiesceKnownAddresses() {
  for (uint8_t addr = 0x0C; addr <= 0x13; ++addr) {
    Wire.beginTransmission(addr);
    Wire.write(MLX90393::CMD_EXIT);
    Wire.endTransmission();
    delay(2);
  }
  delay(10);

  for (uint8_t addr = 0x0C; addr <= 0x13; ++addr) {
    Wire.beginTransmission(addr);
    Wire.write(MLX90393::CMD_RESET);
    Wire.endTransmission();
    delay(2);
  }
  // Datasheet gives 1.5ms for the power-on/reset sequence; be generous.
  delay(20);

  Serial.println(F("[i2c] sent EX+RT to 0x0C..0x13 (software replug)"));
}
