/*
  Minimal I2C scanner for identifying eFlesh magnetometer board variants.

  Plug ONE sensor board directly into the QT Py (no multiplexer), open the
  Serial Monitor at 115200, and read off the addresses. Repeat for each board.

  Expected results:
    0x0C 0x0D 0x0E 0x0F 0x10   -> TARGETS_ALL_CONSEC variant
    0x0C 0x10 0x11 0x12 0x13   -> TARGETS_WHITE_SET variant
  Both sets are already known to eflesh_cmd_stream.ino.

  A multiplexer, if in line, appears at 0x70 (0x70-0x77 depending on jumpers)
  and hides everything downstream until a channel is selected.
*/

#include <Wire.h>

#define Serial SERIAL_PORT_USBVIRTUAL  // SAMD21 (QT Py, Trinket M0)

void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(5); }
  Wire.begin();
  Wire.setClock(400000);
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
