/*
  Minimal I2C scanner for identifying eFlesh magnetometer board variants.
  --- ESP32-C3 port of ../../i2c_scan ---

  Run this BEFORE 5X_eflesh_stream_c3. It confirms the Qwiic wiring and tells
  you whether SDA and SCL are the right way round, and it reports which address
  variant your board is, without having to guess from a silent binary stream.

  Plug ONE sensor board into the C3 (no multiplexer), open the Serial Monitor
  at 115200, and read off the addresses.

  Expected results:
    0x0C 0x0D 0x0E 0x0F 0x10   -> TARGETS_ALL_CONSEC variant
    0x0C 0x10 0x11 0x12 0x13   -> TARGETS_WHITE_SET variant
  Both sets are already known to 5X_eflesh_stream_c3.ino.

  If you see "0 device(s)", swap SDA_PIN and SCL_PIN below and re-upload -- no
  rewiring needed. If you see some but not all five, suspect solder joints on
  the missing chips rather than the bus.

  A multiplexer, if in line, appears at 0x70 (0x70-0x77 depending on jumpers)
  and hides everything downstream until a channel is selected.
*/

#include <Wire.h>

// eFlesh J1 is a JST SH 4-pin (Qwiic/STEMMA QT) connector:
//   pin 1 = GND, pin 2 = VCC (3V3), pins 3 and 4 = the I2C pair.
// GPIO8/GPIO9 are the pins printed as SDA/SCL on the SuperMini pinout card and
// are the ESP32 core's default I2C pair for the C3.
#define SDA_PIN 8
#define SCL_PIN 9

void setup() {
  // Native USB CDC needs a moment to enumerate before the host can open it.
  // The baudrate argument is ignored by USB CDC.
  Serial.begin(115200);
  delay(2000);

  // Free a slave still holding SDA low from a transfer the C3 was reset out of
  // (opening the serial port asserts DTR and reboots it). The magnetometers
  // keep their state across that reset, so a wedged bus survives it too and
  // every probe below would come back empty.
  recoverI2CBus();

  // Scan at 100kHz. Once the chips answer here, the streaming sketch can run
  // the bus at its normal 400kHz.
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);
}

// Nine SCL pulses let a stuck slave finish the byte it was shifting out, then
// a manual STOP releases it. Runs before Wire.begin(), on plain GPIO.
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

  pinMode(SDA_PIN, OUTPUT_OPEN_DRAIN);
  digitalWrite(SDA_PIN, LOW);
  delayMicroseconds(5);
  digitalWrite(SCL_PIN, HIGH);
  delayMicroseconds(5);
  digitalWrite(SDA_PIN, HIGH);
  delayMicroseconds(5);

  pinMode(SDA_PIN, INPUT);
  pinMode(SCL_PIN, INPUT);

  Serial.print(F("[i2c] SDA was "));
  Serial.print(pulses ? F("STUCK LOW, released after ") : F("idle high, no recovery needed ("));
  Serial.print(pulses);
  Serial.println(pulses ? F(" clock pulses") : F(")"));
}

void loop() {
  Serial.println(F("--- scanning 0x08..0x77 ---"));
  uint8_t count = 0;

  for (uint8_t addr = 0x08; addr <= 0x77; ++addr) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.print(F("  found 0x"));
      if (addr < 0x10) Serial.print('0');
      Serial.println(addr, HEX);
      ++count;
    }
  }

  Serial.print(count);
  Serial.println(F(" device(s)"));
  Serial.println();
  delay(2000);
}
