/*
  eFlesh Board Code - human-readable ASCII stream
  Adapted from 5X_eflesh_stream_c3 (binary) and ReSkin's 5X_burst_stream_c3.

  License: This code is public domain but you buy me a beer if you use this and we meet someday (Beerware license).
  Credits: Adapted from (ReSkin (https://reskin.dev) and AnySkin (https://any-skin.github.io) repos) - maintained by Raunaq Bhirangi, Tess Hellebrekers
  Library: Heavily based on original MLX90393 library from Theodore Yapo (https://github.com/tedyapo/arduino-MLX90393)

  Same sensors, same auto-detected address order and same gain / resolution /
  filter configuration as 5X_eflesh_stream_c3 -- the ONLY difference is that
  loop() prints tab-separated decimal text instead of dumping raw structs. Use
  this one for bring-up: the Serial Monitor is readable and the Serial Plotter
  will graph it. Switch back to 5X_eflesh_stream_c3 for the Python library,
  which expects the binary frames.

  Column order is x, y, z, t per chip (20 columns), matching ReSkin's ASCII
  sketch. Note this differs from the binary struct, which is t, x, y, z.

  A header line is printed once at the end of setup() so the Serial Plotter
  labels the traces. That plus the boot banner means this stream is not
  directly parseable by ReSkinBase(burst_mode=False) without skipping the
  non-numeric lines first.
*/

#include <Wire.h>
#include <MLX90393.h>

// eFlesh J1 is a JST SH 4-pin (Qwiic/STEMMA QT) connector:
//   pin 1 = GND, pin 2 = VCC (3V3), pins 3 and 4 = the I2C pair.
// If the chips do not answer, swap these two numbers -- harmless, the bus just
// stays quiet. Run i2c_scan_c3 first to settle it.
//
// GPIO8 also drives the SuperMini's blue LED (flickers with bus traffic,
// cosmetic). GPIO9 is the BOOT strapping pin: do not hold BOOT across a reset
// while the bus is live, or the chip comes up in download mode.
#define SDA_PIN 8
#define SCL_PIN 9

static const uint8_t NUM_SENSORS = 5;
MLX90393 mlx[NUM_SENSORS];
MLX90393::txyz dataBuf[NUM_SENSORS];

// How many of mlx[] actually had begin() called. Indices >= activeCount are
// never touched on the bus: MLX90393's constructor leaves its TwoWire*
// uninitialized, so reading an index that never got begin() dereferences a wild
// pointer and panics the C3. Their buffers stay zeroed, so the column count
// does not change when a chip is missing.
uint8_t activeCount = 0;

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
  // Native USB needs a moment to enumerate. Baudrate is ignored by USB CDC.
  Serial.begin(115200);
  delay(2000);

  // Same three-stage bring-up as the binary sketch: free a stuck bus, take the
  // chips out of the burst mode they are still running from the previous run,
  // then probe at 100kHz. Without this the scan finds nothing until the Qwiic
  // connector is physically unplugged.
  recoverI2CBus();

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
  delay(10);

  quiesceKnownAddresses();

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
    Serial.println(F("[WARN] Did not find exactly 5 target MLX addresses. Proceeding with what was found."));
  }

  for (uint8_t i = 0; i < orderedCount; ++i) {
    byte status = mlx[i].begin(ordered[i], -1, Wire);
    Serial.print(F("Init MLX[")); Serial.print(i); Serial.print(F("] @0x"));
    Serial.print(ordered[i], HEX);
    Serial.print(F(" status=")); Serial.println(status, HEX);

    // Configs -- identical to the binary sketch, so readings are comparable
    mlx[i].setGainSel(0x1);
    mlx[i].setResolution(0x2, 0x2, 0x2);
    mlx[i].setDigitalFiltering(0x4);

    // Start burst mode (Temp + X + Y + Z) = 0xF
    mlx[i].startBurst(0xF);
  }

  activeCount = orderedCount;

  // Chips answer at 100kHz; run the bus at full speed for streaming.
  Wire.setClock(400000);

  Serial.print(F("Streaming order: "));
  for (uint8_t i = 0; i < activeCount; ++i) {
    Serial.print("0x"); Serial.print(ordered[i], HEX); Serial.print(" ");
  }
  Serial.println();

  // Column labels, once. Chips at or past activeCount are the zero-filled ones.
  for (uint8_t i = 0; i < NUM_SENSORS; ++i) {
    Serial.print("x"); Serial.print(i); Serial.print("\t");
    Serial.print("y"); Serial.print(i); Serial.print("\t");
    Serial.print("z"); Serial.print(i); Serial.print("\t");
    Serial.print("t"); Serial.print(i);
    if (i < NUM_SENSORS - 1) Serial.print("\t");
  }
  Serial.println();
}

void loop() {
  // Read only the sensors that were actually initialized.
  for (uint8_t i = 0; i < activeCount; ++i) {
    mlx[i].readBurstData(dataBuf[i]);
  }

  // Write tab-separated text: x, y, z, t per chip
  for (uint8_t i = 0; i < NUM_SENSORS; ++i) {
    Serial.print(dataBuf[i].x); Serial.print("\t");
    Serial.print(dataBuf[i].y); Serial.print("\t");
    Serial.print(dataBuf[i].z); Serial.print("\t");
    Serial.print(dataBuf[i].t);
    if (i < NUM_SENSORS - 1) Serial.print("\t");
  }
  Serial.println();

  // Slower than the binary sketch on purpose: formatting 20 floats as text is
  // far more expensive than a 80-byte struct dump, and nobody can read 2 kHz.
  delay(50);
}

// Scan all I2C addresses to find MLX90393 sensors
void scanI2C(uint8_t* found, uint8_t& count) {
  count = 0;
  for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
    Wire.beginTransmission(addr);
    uint8_t err = Wire.endTransmission();
    if (err == 0) {
      found[count++] = addr;
      if (count >= 16) break;
    }
  }
}

void chooseOrderedAddresses(const uint8_t* found, uint8_t count, uint8_t* ordered, uint8_t& orderedCount) {
  orderedCount = 0;

  if (count >= NUM_SENSORS) {
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
