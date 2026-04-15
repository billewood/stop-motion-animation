/*
 * Stop-motion booth button controller.
 *
 * Wire each momentary push button between a digital pin and GND.
 * INPUT_PULLUP is enabled, so a press reads LOW.
 *
 * On press, sends a single ASCII character over USB serial @ 9600 baud:
 *   G = green   (take picture)
 *   R = red     (delete last picture)
 *   B = blue    (build & play movie)
 *   W = white   (yes / save & upload)
 *   K = black   (no  / keep working)
 *   Y = yellow  (toggle onion skin)
 *
 * Adjust the pin numbers below to match your wiring.
 */

struct Button {
  uint8_t pin;
  char    code;
  uint8_t lastState;
  unsigned long lastChangeMs;
};

Button buttons[] = {
  { 2, 'G', HIGH, 0 },  // green
  { 3, 'R', HIGH, 0 },  // red
  { 4, 'B', HIGH, 0 },  // blue
  { 5, 'W', HIGH, 0 },  // white
  { 6, 'K', HIGH, 0 },  // black
  { 7, 'Y', HIGH, 0 },  // yellow
};

const unsigned long DEBOUNCE_MS = 25;

void setup() {
  Serial.begin(9600);
  for (auto &b : buttons) {
    pinMode(b.pin, INPUT_PULLUP);
    b.lastState = digitalRead(b.pin);
  }
}

void loop() {
  unsigned long now = millis();
  for (auto &b : buttons) {
    uint8_t s = digitalRead(b.pin);
    if (s != b.lastState && (now - b.lastChangeMs) > DEBOUNCE_MS) {
      b.lastChangeMs = now;
      if (b.lastState == HIGH && s == LOW) {   // press edge
        Serial.write(b.code);
      }
      b.lastState = s;
    }
  }
}
