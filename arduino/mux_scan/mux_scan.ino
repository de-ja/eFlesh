/*
  Mux-aware I2C scanner for multi-board eFlesh setups.

  Finds a TCA9548A / PCA9548A / PCA9546A multiplexer, then scans every
  downstream channel separately and identifies which eFlesh address variant
  is on each one.

  Wire the mux to the QT Py's STEMMA connector and each eFlesh board to a
  downstream channel. Open the Serial Monitor at 115200.

  Why this is needed: every eFlesh board carries the same five magnetometer
  addresses, and the MLX90393 address is fixed by order code plus the A0/A1
  strap pins -- there is no address register to write. Two boards on one bus
  therefore collide, and the collision is silent: both chips ACK, both drive
  SDA on a read, and the scan still reports five addresses while the data is
  the wired-AND of two sensors. A mux is the fix.
*/

#include <Wire.h>

#define Serial SERIAL_PORT_USBVIRTUAL  // SAMD21 (QT Py, Trinket M0)

const uint8_t MUX_ADDR_LO = 0x70;
const uint8_t MUX_ADDR_HI = 0x77;
const uint8_t NUM_CHANNELS = 8;   // PCA9546A only has 4; extra ones read empty

const uint8_t TARGETS_ALL_CONSEC[5] = {0x0C, 0x0D, 0x0E, 0x0F, 0x10};
const uint8_t TARGETS_WHITE_SET[5]  = {0x0C, 0x10, 0x11, 0x12, 0x13};

uint8_t muxAddr = 0;

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool muxWrite(uint8_t mask) {
  Wire.beginTransmission(muxAddr);
  Wire.write(mask);
  return Wire.endTransmission() == 0;
}

void printAddr(uint8_t a) {
  Serial.print(F("0x"));
  if (a < 0x10) Serial.print('0');
  Serial.print(a, HEX);
}

// Scan one bus, skipping the mux's own address. Returns count, fills found[].
uint8_t scanBus(uint8_t* found) {
  uint8_t n = 0;
  for (uint8_t addr = 0x08; addr <= 0x77; ++addr) {
    if (addr == muxAddr) continue;          // always ACKs, not downstream
    if (present(addr) && n < 16) found[n++] = addr;
  }
  return n;
}

bool isSet(const uint8_t* found, uint8_t n, const uint8_t* pattern) {
  if (n != 5) return false;
  for (uint8_t p = 0; p < 5; ++p) {
    bool hit = false;
    for (uint8_t i = 0; i < n; ++i) if (found[i] == pattern[p]) { hit = true; break; }
    if (!hit) return false;
  }
  return true;
}

void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(5); }
  Wire.begin();
  Wire.setClock(100000);   // slow for discovery; the stream sketch uses 400k
  delay(50);
}

void loop() {
  Serial.println(F("=== mux scan ==="));

  // 1. Find the mux.
  muxAddr = 0;
  for (uint8_t a = MUX_ADDR_LO; a <= MUX_ADDR_HI; ++a) {
    if (present(a)) {
      if (muxAddr == 0) {
        muxAddr = a;
      } else {
        Serial.print(F("[WARN] second device in mux range at ")); printAddr(a); Serial.println();
      }
    }
  }

  if (muxAddr == 0) {
    Serial.println(F("No mux found in 0x70-0x77."));
    Serial.println(F("Scanning the bus directly instead:"));
    uint8_t found[16];
    uint8_t n = scanBus(found);
    Serial.print(F("  "));
    for (uint8_t i = 0; i < n; ++i) { printAddr(found[i]); Serial.print(' '); }
    Serial.print(F("  ("));
    Serial.print(n);
    Serial.println(F(" device(s))"));
    Serial.println();
    delay(3000);
    return;
  }

  Serial.print(F("Mux at ")); printAddr(muxAddr); Serial.println();

  // 2. All channels off -- anything still visible is on the trunk, not a channel.
  muxWrite(0x00);
  {
    uint8_t found[16];
    uint8_t n = scanBus(found);
    Serial.print(F("Trunk (all channels off): "));
    if (n == 0) {
      Serial.println(F("clear"));
    } else {
      for (uint8_t i = 0; i < n; ++i) { printAddr(found[i]); Serial.print(' '); }
      Serial.println(F(" <- these share the bus with every board"));
    }
  }

  // 3. One channel at a time.
  uint8_t boards = 0;
  for (uint8_t ch = 0; ch < NUM_CHANNELS; ++ch) {
    if (!muxWrite(1 << ch)) {
      Serial.print(F("  ch")); Serial.print(ch); Serial.println(F(": mux write failed"));
      continue;
    }
    delay(2);

    uint8_t found[16];
    uint8_t n = scanBus(found);
    if (n == 0) continue;   // stay quiet about empty channels

    Serial.print(F("  ch")); Serial.print(ch); Serial.print(F(": "));
    for (uint8_t i = 0; i < n; ++i) { printAddr(found[i]); Serial.print(' '); }

    if (isSet(found, n, TARGETS_ALL_CONSEC))     { Serial.print(F(" -> eFlesh, consecutive variant")); ++boards; }
    else if (isSet(found, n, TARGETS_WHITE_SET)) { Serial.print(F(" -> eFlesh, white variant")); ++boards; }
    else if (n == 5)                             { Serial.print(F(" -> 5 devices, unrecognised set")); }
    else                                         { Serial.print(F(" -> ")); Serial.print(n); Serial.print(F(" device(s), not a full board")); }
    Serial.println();
  }

  muxWrite(0x00);
  Serial.print(boards);
  Serial.println(F(" eFlesh board(s) found."));
  Serial.println();
  delay(3000);
}
