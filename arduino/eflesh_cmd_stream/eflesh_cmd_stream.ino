/*
  eFlesh stream with live settings, for the QT Py M0 / Trinket M0.

  One sketch for both wirings: if an I2C mux answers in 0x70-0x77 every
  populated channel is a board (ascending channel order), otherwise one board is
  read straight off the bus. Up to MAX_BOARDS boards, five magnetometers each.

  At boot it behaves exactly like 5X_eflesh_stream / 10X_eflesh_mux_stream: a
  text banner, then binary frames, so anyskin and eflesh.EFleshSource work
  unchanged. On top of that it accepts text commands, one per line, so a host
  can retune the chips without re-flashing.

  Wire format (binary mode), per frame:
    boards * 5 magnetometers * (float t, x, y, z) little-endian, then \r\n
  ASCII mode prints one tab-separated row per frame instead, x y z t per chip,
  after a header row, for the Serial Monitor / Plotter.

  Commands (case-insensitive, \n-terminated, replies end in \r\n):
    STOP              stop streaming; always answered with "#STOPPED"
    START             "OK START", then streaming resumes in the current mode
    INFO              "OK INFO fw=... mux=... boards=... mags=... frame_bytes=... b0=..."
    PARAMS            one "P name value default min max description" line per
                      setting, then "OK PARAMS <count>". This table is the
                      source of truth: hosts build their controls from it.
    GET               "OK GET k=v ..." -- every setting plus derived values
    SET k=v [k=v ...] validate all, apply all, then answer like GET. Nothing is
                      applied if any pair is rejected ("ERR ...").
    DEFAULTS          restore every setting to its default, answer like GET
    STATS             "OK STATS frames=N ms=M fails=a,b,..." since the last STATS
    HELP              list the commands

  In binary mode any command other than STOP stops the stream first and prints
  "#STOPPED", so a reply is never spliced into the middle of a binary frame.
  Commands are only read between frames, so "#STOPPED" always follows a complete
  frame. In ASCII mode commands are answered inline and the stream continues.

  Settings live in RAM and reset to the defaults on power-up.

  Derived values in GET (from the MLX90393 datasheet, HALLCONF = 0xC):
    lsb_x/y/z    microtesla per LSB
    range_x/y/z  representable output range, +/- millitesla
    conv_us      time one chip needs for T + X + Y + Z, which sets the frame
                 rate: one frame per conv_us + period_us

  The chips run single measurements, not burst mode: each is read and at once
  told to measure again, then left alone for conv_us. Every frame therefore
  holds a fresh sample from every chip. In burst mode the host cannot tell a
  fresh sample from a repeat, and reads that land mid-conversion return
  0xFFFF; at slow settings some chips never deliver at all.

  STATS fails count reads that came back without data (I2C error, the status
  ERROR bit, or raw 0xFFFF); the frame repeats that chip's last good sample.
  At any setting this should stay at 0.

  Adapted from 5X_eflesh_stream.ino (Venkatesh P) and 10X_eflesh_mux_stream.ino,
  same Beerware license.
  Library: https://github.com/tesshellebrekers/arduino-MLX90393  (required)
*/

#include <Wire.h>
#include <MLX90393.h>
#include <string.h>
#include <stdlib.h>

#define Serial SERIAL_PORT_USBVIRTUAL  // SAMD21 (QT Py, Trinket M0)

static const char    FW_ID[]        = "eflesh_cmd/1";
static const uint8_t MAGS_PER_BOARD = 5;
static const uint8_t MAX_BOARDS     = 2;   // raise if you add channels
static const uint8_t NUM_CHANNELS   = 8;
static const uint8_t DIRECT         = 0xFF; // boardChan value when there is no mux

const uint8_t TARGETS_ALL_CONSEC[MAGS_PER_BOARD] = {0x0C, 0x0D, 0x0E, 0x0F, 0x10};
const uint8_t TARGETS_WHITE_SET[MAGS_PER_BOARD]  = {0x0C, 0x10, 0x11, 0x12, 0x13};

MLX90393        mlx[MAX_BOARDS][MAGS_PER_BOARD];
MLX90393::txyz  dataBuf[MAX_BOARDS][MAGS_PER_BOARD];
uint8_t         addrs[MAX_BOARDS][MAGS_PER_BOARD];  // 0 = slot empty, never touched on the bus
uint8_t         boardChan[MAX_BOARDS];
uint8_t         numBoards = 0;
uint8_t         muxAddr   = 0;                      // 0 = no mux

uint32_t fails[MAX_BOARDS][MAGS_PER_BOARD];
uint32_t startedAt[MAX_BOARDS][MAGS_PER_BOARD];     // micros() of the last start-measurement
uint32_t frames     = 0;
uint32_t statsSince = 0;
bool     streaming  = false;

// ---------------------------------------------------------------- settings

struct Param {
  const char* name;
  int32_t     value;
  int32_t     def;
  int32_t     lo, hi;
  const char* desc;
};

enum { P_GAIN, P_RES_X, P_RES_Y, P_RES_Z, P_FILT, P_OSR, P_OSR2, P_TCMP,
       P_PERIOD, P_ASCII, NUM_PARAMS, NUM_CHIP_PARAMS = P_PERIOD };

// Chip defaults match the original sketches: gain 1, resolution 2, filter 4, OSR 0.
Param params[NUM_PARAMS] = {
  {"gain",      1,   1,   0, 7,      "Analog gain GAIN_SEL: 0 = lowest gain (widest range), 7 = highest"},
  {"res_x",     2,   2,   0, 3,      "X output resolution RES_X: each step doubles uT/LSB and range"},
  {"res_y",     2,   2,   0, 3,      "Y output resolution RES_Y: each step doubles uT/LSB and range"},
  {"res_z",     2,   2,   0, 3,      "Z output resolution RES_Z: each step doubles uT/LSB and range"},
  {"filt",      4,   4,   0, 7,      "Digital filter DIG_FILT: higher = less noise, slower conversion"},
  {"osr",       0,   0,   0, 3,      "Magnetic oversampling OSR: higher = less noise, slower conversion"},
  {"osr2",      0,   0,   0, 3,      "Temperature oversampling OSR2: higher = less noise, slower conversion"},
  {"tcmp",      0,   0,   0, 1,      "On-chip temperature compensation TCMP_EN"},
  {"period_us", 0,   0,   0, 100000, "Extra wait between frames on top of the conversion time, microseconds"},
  {"ascii",     0,   0,   0, 1,      "Output: 0 = binary frames (anyskin), 1 = tab-separated text (Serial Plotter)"},
};

int findParam(const char* name) {
  for (uint8_t i = 0; i < NUM_PARAMS; ++i)
    if (strcasecmp(name, params[i].name) == 0) return i;
  return -1;
}

// MLX90393 datasheet: these OSR / DIG_FILT pairs are not permitted.
bool comboAllowed(int32_t filt, int32_t osr) {
  if (filt == 0 && osr <= 1) return false;
  if (filt == 1 && osr == 0) return false;
  return true;
}

// Library constants: sensitivity at HALLCONF = 0xC and gain multipliers.
const float BASE_XY_UT = 0.150f;
const float BASE_Z_UT  = 0.242f;
const float GAIN_MULT[8] = {5.f, 4.f, 3.f, 2.5f, 2.f, 1.66666667f, 1.33333333f, 1.f};

float lsbUT(uint8_t axis) {   // 0 = x, 1 = y, 2 = z
  float base = (axis == 2) ? BASE_Z_UT : BASE_XY_UT;
  return base * GAIN_MULT[params[P_GAIN].value] * (1 << params[P_RES_X + axis].value);
}

// Matches the library's convertRaw: offset 16384 at RES 3 without TCMP, else 32768.
float rangeMT(uint8_t axis) {
  bool half = params[P_RES_X + axis].value == 3 && !params[P_TCMP].value;
  return (half ? 16384.f : 32768.f) * lsbUT(axis) / 1000.f;
}

// Datasheet TCONVM per axis plus TCONVT for temperature, microseconds. Real
// chips need ~0.55 ms more than the datasheet sum at every setting (measured
// on a QT Py board), so add 0.6 ms plus 3 % for clock spread.
uint32_t convMicros() {
  uint32_t osr = params[P_OSR].value, filt = params[P_FILT].value, osr2 = params[P_OSR2].value;
  uint32_t perAxis = 67 + 64 * (1UL << osr) * (2 + (1UL << filt));
  uint32_t temp    = 67 + 192 * (1UL << osr2);
  uint32_t sheet   = 3 * perAxis + temp;
  return sheet + sheet / 32 + 600;
}

// ---------------------------------------------------------------- bus

bool selectChannel(uint8_t ch) {
  if (ch == DIRECT) return true;
  Wire.beginTransmission(muxAddr);
  Wire.write((uint8_t)(1 << ch));
  return Wire.endTransmission() == 0;
}

void deselectAll() {
  if (!muxAddr) return;
  Wire.beginTransmission(muxAddr);
  Wire.write((uint8_t)0x00);
  Wire.endTransmission();
}

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void printHex2(uint8_t a) {
  if (a < 0x10) Serial.print('0');
  Serial.print(a, HEX);
}

// ---------------------------------------------------------------- discovery

// Take every chip in the MLX90393 range out of burst mode. Magnetometers stay
// powered across a microcontroller reset and can come back mid-burst holding
// SDA low, which makes the scan find nothing. EX then RT is a software replug.
void quiesce() {
  for (uint8_t addr = 0x0C; addr <= 0x13; ++addr) {
    Wire.beginTransmission(addr);
    Wire.write((uint8_t)0x80);        // CMD_EXIT
    Wire.endTransmission();
    delay(2);
    Wire.beginTransmission(addr);
    Wire.write((uint8_t)0xF0);        // CMD_RESET
    Wire.endTransmission();
    delay(2);
  }
}

uint8_t scanMLX(uint8_t* found) {
  uint8_t n = 0;
  for (uint8_t addr = 0x0C; addr <= 0x13; ++addr)
    if (present(addr)) found[n++] = addr;
  return n;
}

bool isSet(const uint8_t* found, uint8_t n, const uint8_t* pattern) {
  if (n != MAGS_PER_BOARD) return false;
  for (uint8_t p = 0; p < MAGS_PER_BOARD; ++p) {
    bool hit = false;
    for (uint8_t i = 0; i < n; ++i) if (found[i] == pattern[p]) { hit = true; break; }
    if (!hit) return false;
  }
  return true;
}

// Fill `ordered` in the canonical stream order: middle, left, right, top,
// bottom. An unrecognised set keeps ascending address order and empty slots
// stay 0, so the frame width never depends on how many chips answered.
// Returns true for a recognised full board.
bool orderAddresses(const uint8_t* found, uint8_t n, uint8_t* ordered) {
  if (isSet(found, n, TARGETS_ALL_CONSEC)) {
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) ordered[i] = TARGETS_ALL_CONSEC[i];
    return true;
  }
  if (isSet(found, n, TARGETS_WHITE_SET)) {
    // Not ascending: 0x0C, 0x11, 0x12, 0x13, 0x10 is middle, left, right, top, bottom.
    ordered[0] = 0x0C; ordered[1] = 0x11; ordered[2] = 0x12;
    ordered[3] = 0x13; ordered[4] = 0x10;
    return true;
  }
  for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) ordered[i] = (i < n) ? found[i] : 0;
  return false;
}

// Record whatever is on the currently selected channel as the next board.
void addBoard(uint8_t ch) {
  quiesce();
  uint8_t found[8];
  uint8_t n = scanMLX(found);
  if (n == 0) return;

  bool known = orderAddresses(found, n, addrs[numBoards]);
  boardChan[numBoards] = ch;

  Serial.print(F("Board ")); Serial.print(numBoards);
  if (ch == DIRECT) Serial.print(F(" direct: "));
  else { Serial.print(F(" on ch")); Serial.print(ch); Serial.print(F(": ")); }
  for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
    Serial.print(F("0x")); printHex2(addrs[numBoards][i]); Serial.print(' ');
  }
  if (!known) Serial.print(F(" [WARN] unrecognised set, ascending order"));
  Serial.println();
  ++numBoards;
}

// ---------------------------------------------------------------- chips

// After EX the chip still answers commands such as NOP, but register reads
// fail until the conversion it was running ends: ~4 ms at the defaults, ~200 ms
// at the slowest settings. Poll a register read until it succeeds.
uint8_t waitIdle(MLX90393& m) {
  for (uint16_t ms = 0; ms < 500; ++ms) {
    uint16_t v;
    if (m.checkStatus(m.readRegister(0, v)) == MLX90393::STATUS_OK) return MLX90393::STATUS_OK;
    delay(1);
  }
  return MLX90393::STATUS_ERROR;
}

const uint8_t TXYZ = MLX90393::X_FLAG | MLX90393::Y_FLAG | MLX90393::Z_FLAG | MLX90393::T_FLAG;

// Start a measurement on every chip. One still busy with the last one rejects
// the start, and its next read collects that measurement instead.
void startAll() {
  for (uint8_t b = 0; b < numBoards; ++b) {
    selectChannel(boardChan[b]);
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      if (!addrs[b][i]) continue;
      mlx[b][i].startMeasurement(TXYZ);
      startedAt[b][i] = micros();
    }
  }
}

// Push the chip settings to every magnetometer. Each chip abandons any
// measurement in flight, takes the new registers and is read back. Returns the
// number of chips that failed; their details go to `errOut` for the reply.
uint8_t applyChipConfig(char* errOut, size_t errLen) {
  uint8_t bad = 0;
  errOut[0] = 0;
  for (uint8_t b = 0; b < numBoards; ++b) {
    selectChannel(boardChan[b]);
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      if (!addrs[b][i]) continue;
      MLX90393& m = mlx[b][i];
      uint8_t s = MLX90393::STATUS_OK;
      s |= m.checkStatus(m.exit());
      s |= waitIdle(m);
      s |= m.setGainSel(params[P_GAIN].value);
      s |= m.setResolution(params[P_RES_X].value, params[P_RES_Y].value, params[P_RES_Z].value);
      s |= m.setOverSampling(params[P_OSR].value);
      s |= m.setDigitalFiltering(params[P_FILT].value);
      s |= m.setTemperatureOverSampling(params[P_OSR2].value);
      s |= m.setTemperatureCompensation(params[P_TCMP].value);

      uint8_t gain, rx, ry, rz, osr, filt, osr2, tcmp;
      s |= m.getGainSel(gain);
      s |= m.getResolution(rx, ry, rz);
      s |= m.getOverSampling(osr);
      s |= m.getDigitalFiltering(filt);
      s |= m.getTemperatureOverSampling(osr2);
      s |= m.getTemperatureCompensation(tcmp);
      bool match = gain == params[P_GAIN].value && rx == params[P_RES_X].value &&
                   ry == params[P_RES_Y].value && rz == params[P_RES_Z].value &&
                   osr == params[P_OSR].value && filt == params[P_FILT].value &&
                   osr2 == params[P_OSR2].value && tcmp == params[P_TCMP].value;

      if (s != MLX90393::STATUS_OK || !match) {
        if (!bad) snprintf(errOut, errLen, "chip b%u.%u @0x%02X %s",
                           b, i, addrs[b][i], match ? "i2c error" : "readback mismatch");
        ++bad;
      }
    }
  }
  deselectAll();
  if (streaming) startAll();   // ASCII mode keeps streaming through a SET
  return bad;
}

// Read every chip and restart it straight away, so each one gets a full
// conversion before its next read. Chips are visited in the same order every
// time, so the wait below is near zero; it only matters if the bus stalls.
void readAll() {
  uint32_t conv = convMicros();
  for (uint8_t b = 0; b < numBoards; ++b) {
    selectChannel(boardChan[b]);
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      if (!addrs[b][i]) continue;
      MLX90393& m = mlx[b][i];
      while (micros() - startedAt[b][i] < conv) {}
      MLX90393::txyzRaw raw;
      uint8_t st = m.readMeasurement(TXYZ, raw);
      m.startMeasurement(TXYZ);
      startedAt[b][i] = micros();
      // On a failed read hold the last good sample: raw 0xFFFF converts to a
      // huge bogus value that poisons baselines.
      if (st != MLX90393::STATUS_ERROR && !(st & MLX90393::ERROR_BIT) &&
          raw.x != 0xFFFF && raw.y != 0xFFFF && raw.z != 0xFFFF)
        dataBuf[b][i] = m.convertRaw(raw);
      else ++fails[b][i];
    }
  }
}

// The first chip in the frame sets the pace; the rest were started just after it.
bool frameDue() {
  for (uint8_t b = 0; b < numBoards; ++b)
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i)
      if (addrs[b][i])
        return micros() - startedAt[b][i] >= convMicros() + (uint32_t)params[P_PERIOD].value;
  return false;
}

// ---------------------------------------------------------------- output

void writeBinary() {
  for (uint8_t b = 0; b < numBoards; ++b)
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i)
      Serial.write((uint8_t*)&dataBuf[b][i], sizeof(dataBuf[b][i]));
  Serial.println();
}

void writeAsciiHeader() {
  uint8_t n = numBoards * MAGS_PER_BOARD;
  for (uint8_t k = 0; k < n; ++k) {
    Serial.print('x'); Serial.print(k); Serial.print('\t');
    Serial.print('y'); Serial.print(k); Serial.print('\t');
    Serial.print('z'); Serial.print(k); Serial.print('\t');
    Serial.print('t'); Serial.print(k);
    if (k < n - 1) Serial.print('\t');
  }
  Serial.println();
}

void writeAscii() {
  for (uint8_t b = 0; b < numBoards; ++b) {
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      const MLX90393::txyz& d = dataBuf[b][i];
      Serial.print(d.x); Serial.print('\t');
      Serial.print(d.y); Serial.print('\t');
      Serial.print(d.z); Serial.print('\t');
      Serial.print(d.t);
      if (b < numBoards - 1 || i < MAGS_PER_BOARD - 1) Serial.print('\t');
    }
  }
  Serial.println();
}

// ---------------------------------------------------------------- replies

void printState(const __FlashStringHelper* tag) {
  Serial.print(F("OK ")); Serial.print(tag);
  for (uint8_t i = 0; i < NUM_PARAMS; ++i) {
    Serial.print(' '); Serial.print(params[i].name);
    Serial.print('='); Serial.print(params[i].value);
  }
  const char axes[3] = {'x', 'y', 'z'};
  for (uint8_t a = 0; a < 3; ++a) {
    Serial.print(F(" lsb_")); Serial.print(axes[a]); Serial.print('='); Serial.print(lsbUT(a), 4);
  }
  for (uint8_t a = 0; a < 3; ++a) {
    Serial.print(F(" range_")); Serial.print(axes[a]); Serial.print('='); Serial.print(rangeMT(a), 2);
  }
  Serial.print(F(" conv_us=")); Serial.println(convMicros());
}

void printInfo() {
  Serial.print(F("OK INFO fw=")); Serial.print(FW_ID);
  Serial.print(F(" mux="));
  if (muxAddr) { Serial.print(F("0x")); printHex2(muxAddr); } else Serial.print(F("none"));
  Serial.print(F(" boards=")); Serial.print(numBoards);
  Serial.print(F(" mags=")); Serial.print(numBoards * MAGS_PER_BOARD);
  Serial.print(F(" frame_bytes=")); Serial.print(numBoards * MAGS_PER_BOARD * 16 + 2);
  for (uint8_t b = 0; b < numBoards; ++b) {
    Serial.print(F(" b")); Serial.print(b); Serial.print('=');
    if (boardChan[b] == DIRECT) Serial.print(F("direct"));
    else { Serial.print(F("ch")); Serial.print(boardChan[b]); }
    Serial.print(':');
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      printHex2(addrs[b][i]);
      if (i < MAGS_PER_BOARD - 1) Serial.print(',');
    }
  }
  Serial.println();
}

void printParams() {
  for (uint8_t i = 0; i < NUM_PARAMS; ++i) {
    const Param& p = params[i];
    Serial.print(F("P ")); Serial.print(p.name);
    Serial.print(' '); Serial.print(p.value);
    Serial.print(' '); Serial.print(p.def);
    Serial.print(' '); Serial.print(p.lo);
    Serial.print(' '); Serial.print(p.hi);
    Serial.print(' '); Serial.println(p.desc);
  }
  Serial.print(F("OK PARAMS ")); Serial.println(NUM_PARAMS);
}

void printStats() {
  uint32_t now = millis();
  Serial.print(F("OK STATS frames=")); Serial.print(frames);
  Serial.print(F(" ms=")); Serial.print(now - statsSince);
  Serial.print(F(" fails="));
  for (uint8_t b = 0; b < numBoards; ++b) {
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      Serial.print(fails[b][i]);
      fails[b][i] = 0;
      if (b < numBoards - 1 || i < MAGS_PER_BOARD - 1) Serial.print(',');
    }
  }
  Serial.println();
  frames = 0;
  statsSince = now;
}

void printHelp() {
  Serial.println(F("# commands: STOP START INFO PARAMS GET SET k=v.. DEFAULTS STATS HELP"));
  Serial.println(F("OK HELP"));
}

// Apply `staged` if it validates. On success the reply is the full state.
void commit(const int32_t* staged, const __FlashStringHelper* tag) {
  if (!comboAllowed(staged[P_FILT], staged[P_OSR])) {
    Serial.print(F("ERR filt=")); Serial.print(staged[P_FILT]);
    Serial.print(F(" with osr=")); Serial.print(staged[P_OSR]);
    Serial.println(F(" is not permitted by the MLX90393"));
    return;
  }
  bool chipChanged = false;
  for (uint8_t i = 0; i < NUM_PARAMS; ++i) {
    if (i < NUM_CHIP_PARAMS && staged[i] != params[i].value) chipChanged = true;
    params[i].value = staged[i];
  }
  if (chipChanged) {
    char err[48];
    uint8_t bad = applyChipConfig(err, sizeof(err));
    if (bad) {
      Serial.print(F("ERR ")); Serial.print(bad);
      Serial.print(F(" chip(s) failed to configure, first: ")); Serial.println(err);
      return;
    }
  }
  printState(tag);
}

void handleSet(char* rest) {
  int32_t staged[NUM_PARAMS];
  for (uint8_t i = 0; i < NUM_PARAMS; ++i) staged[i] = params[i].value;

  uint8_t pairs = 0;
  for (char* tok = strtok(rest, " \t"); tok; tok = strtok(nullptr, " \t")) {
    char* eq = strchr(tok, '=');
    if (!eq) { Serial.print(F("ERR expected k=v, got ")); Serial.println(tok); return; }
    *eq = 0;
    int idx = findParam(tok);
    if (idx < 0) { Serial.print(F("ERR unknown setting ")); Serial.println(tok); return; }
    char* end;
    long v = strtol(eq + 1, &end, 0);
    if (end == eq + 1 || *end) { Serial.print(F("ERR bad number for ")); Serial.println(tok); return; }
    if (v < params[idx].lo || v > params[idx].hi) {
      Serial.print(F("ERR ")); Serial.print(params[idx].name);
      Serial.print(F(" must be ")); Serial.print(params[idx].lo);
      Serial.print(F("..")); Serial.println(params[idx].hi);
      return;
    }
    staged[idx] = v;
    ++pairs;
  }
  if (!pairs) { Serial.println(F("ERR SET needs at least one k=v")); return; }
  commit(staged, F("SET"));
}

void handleLine(char* line) {
  char* cmd = strtok(line, " \t");
  if (!cmd) return;

  if (strcasecmp(cmd, "STOP") == 0) {
    streaming = false;
    Serial.println(F("#STOPPED"));
    return;
  }

  // Never splice a reply into a binary stream.
  if (streaming && !params[P_ASCII].value) {
    streaming = false;
    Serial.println(F("#STOPPED"));
  }

  if (strcasecmp(cmd, "START") == 0) {
    if (!numBoards) { Serial.println(F("ERR no boards found at boot")); return; }
    Serial.println(F("OK START"));
    if (params[P_ASCII].value) writeAsciiHeader();
    startAll();
    streaming = true;
  }
  else if (strcasecmp(cmd, "INFO") == 0)   printInfo();
  else if (strcasecmp(cmd, "PARAMS") == 0) printParams();
  else if (strcasecmp(cmd, "GET") == 0)    printState(F("GET"));
  else if (strcasecmp(cmd, "STATS") == 0)  printStats();
  else if (strcasecmp(cmd, "HELP") == 0)   printHelp();
  else if (strcasecmp(cmd, "SET") == 0) {
    char* rest = strtok(nullptr, "");
    if (rest) handleSet(rest);
    else Serial.println(F("ERR SET needs at least one k=v"));
  }
  else if (strcasecmp(cmd, "DEFAULTS") == 0) {
    int32_t staged[NUM_PARAMS];
    for (uint8_t i = 0; i < NUM_PARAMS; ++i) staged[i] = params[i].def;
    commit(staged, F("DEFAULTS"));
  }
  else { Serial.print(F("ERR unknown command ")); Serial.println(cmd); }
}

void pollCommands() {
  static char line[128];
  static uint8_t len = 0;
  static bool overflow = false;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[len] = 0;
      if (overflow) Serial.println(F("ERR line too long"));
      else if (len) handleLine(line);
      len = 0;
      overflow = false;
    } else if (len < sizeof(line) - 1) {
      line[len++] = c;
    } else {
      overflow = true;
    }
  }
}

// ---------------------------------------------------------------- setup

void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(5); }

  Wire.begin();
  Wire.setClock(100000);   // slow during discovery
  delay(50);

  for (uint8_t a = 0x70; a <= 0x77; ++a) {
    if (present(a)) { muxAddr = a; break; }
  }

  if (muxAddr) {
    Serial.print(F("Mux at 0x")); printHex2(muxAddr); Serial.println();
    deselectAll();
    for (uint8_t ch = 0; ch < NUM_CHANNELS && numBoards < MAX_BOARDS; ++ch) {
      if (!selectChannel(ch)) continue;
      delay(2);
      addBoard(ch);
    }
    deselectAll();
  } else {
    Serial.println(F("No mux, reading one board on the main bus"));
    addBoard(DIRECT);
  }

  if (numBoards == 0) {
    Serial.println(F("[FATAL] No magnetometers found. Run i2c_scan or mux_scan. Still answering INFO."));
    return;
  }

  Wire.setClock(400000);

  for (uint8_t b = 0; b < numBoards; ++b) {
    selectChannel(boardChan[b]);
    delay(2);
    for (uint8_t i = 0; i < MAGS_PER_BOARD; ++i) {
      if (!addrs[b][i]) continue;
      byte status = mlx[b][i].begin(addrs[b][i], -1, Wire);
      Serial.print(F("  mlx[")); Serial.print(b); Serial.print(F("][")); Serial.print(i);
      Serial.print(F("] @0x")); printHex2(addrs[b][i]);
      Serial.print(F(" status=")); Serial.println(status, HEX);
    }
  }
  deselectAll();

  char err[48];
  if (applyChipConfig(err, sizeof(err))) {
    Serial.print(F("[WARN] configure failed, first: ")); Serial.println(err);
  }

  Serial.print(F("Streaming ")); Serial.print(numBoards * MAGS_PER_BOARD);
  Serial.print(F(" magnetometers, ")); Serial.print(numBoards * MAGS_PER_BOARD * 16 + 2);
  Serial.println(F(" bytes/frame. Send HELP for commands."));

  statsSince = millis();
  startAll();
  streaming = true;
}

// ---------------------------------------------------------------- loop

void loop() {
  pollCommands();
  if (!streaming || !frameDue()) return;   // commands stay live during long conversions

  readAll();
  if (params[P_ASCII].value) writeAscii();
  else writeBinary();
  ++frames;
}
