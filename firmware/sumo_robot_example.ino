/*
 * ============================================================
 *  MİNİ SUMO ROBOT — ÖRNEK FIRMWARE (Arduino Nano / ATmega328P)
 * ============================================================
 *  Donanım ve pin haritası:
 *    QTR-1A  (alt, analog yansıma)  : A0 (sol), A1 (sağ)
 *    E3Z-D81 (ön, dijital mesafe)   : D2 (sol), D3 (sağ)
 *    Motor sürücü  sol  : PWM D9  + yön D7/D8
 *    Motor sürücü  sağ  : PWM D10 + yön D4/D12
 *    Motor enable       : D6
 *    DIP switch (2 konumlu x3)      : A2, A3, A4  (INPUT_PULLUP, kapalı=aktif)
 *    Pil ölçüm (3S LiPo, opsiyonel) : A5
 *    Başlat butonu (opsiyonel)      : D11
 *
 *  Bu kodu değiştirerek robotunun stratejisini geliştir.
 *  Web panelinden "Derle & Başlat" ile robotunu ringe çıkarabilirsin.
 * ============================================================
 */

#define QTR_L  A0
#define QTR_R  A1
#define E3Z_L  2
#define E3Z_R  3

#define ML_PWM 9
#define MR_PWM 10
#define ML_A   7
#define ML_B   8
#define MR_A   4
#define MR_B   12
#define MEN    6

#define DIP1   A2
#define DIP2   A3
#define DIP3   A4

// Yansıma eşikleri (ölçüme göre ayarla)
#define EDGE_THRESHOLD 350   // bu değerin altı = siyah kenar

// Durum değişkenleri
int mod = 0;             // DIP switch ile seçilen mod
unsigned long sonGorulme = 0;

void motorKur() {
  pinMode(ML_A, OUTPUT); pinMode(ML_B, OUTPUT);
  pinMode(MR_A, OUTPUT); pinMode(MR_B, OUTPUT);
  pinMode(MEN, OUTPUT);
  digitalWrite(MEN, HIGH);         // sürücüyü etkinleştir
}

// Motor hızları: -255..255 (negatif = geri)
void motor(int solHiz, int sagHiz) {
  digitalWrite(ML_A, solHiz >= 0); digitalWrite(ML_B, solHiz < 0);
  digitalWrite(MR_A, sagHiz >= 0); digitalWrite(MR_B, sagHiz < 0);
  analogWrite(ML_PWM, constrain(abs(solHiz), 0, 255));
  analogWrite(MR_PWM, constrain(abs(sagHiz), 0, 255));
}

void setup() {
  motorKur();
  pinMode(E3Z_L, INPUT); pinMode(E3Z_R, INPUT);
  pinMode(DIP1, INPUT_PULLUP);
  pinMode(DIP2, INPUT_PULLUP);
  pinMode(DIP3, INPUT_PULLUP);
  motor(0, 0);
}

void loop() {
  // --- Girişleri oku ---
  int ql = analogRead(QTR_L);
  int qr = analogRead(QTR_R);
  bool rakipSol  = digitalRead(E3Z_L);
  bool rakipSag  = digitalRead(E3Z_R);

  bool d1 = !digitalRead(DIP1);
  bool d2 = !digitalRead(DIP2);
  bool d3 = !digitalRead(DIP3);
  mod = (d1 ? 1 : 0) + (d2 ? 2 : 0) + (d3 ? 4 : 0);

  // --- 1) Kenar (dohyo dışı) koruması: her modda öncelikli ---
  bool solKenar = ql < EDGE_THRESHOLD;
  bool sagKenar = qr < EDGE_THRESHOLD;
  if (solKenar && sagKenar) {
    motor(-220, -220);              // tam geri çekil
    delay(60);
    return;
  }
  if (solKenar) { motor(160, -160); delay(40); return; }   // sola dön
  if (sagKenar) { motor(-160, 160); delay(40); return; }   // sağa dön

  // --- 2) Rakip tespiti ---
  if (rakipSol || rakipSag) {
    sonGorulme = millis();
    if (mod == 2) {
      // Savunma modu (DIP2): dokunup geri çekil
      motor(200, 200);
    } else {
      // Saldırı: tam güç ileri
      motor(255, 255);
    }
    delay(5);
    return;
  }

  // --- 3) Rakip görünmüyorsa: moda göre tarama ---
  switch (mod) {
    case 1:                          // DIP1: sola tarama (geniş yay)
      motor(190, 130);
      break;
    case 4:                          // DIP3: sağa tarama (geniş yay)
      motor(130, 190);
      break;
    case 2:                          // DIP2: yavaş bekle (savunma)
      motor(90, 90);
      break;
    default:                         // mod 0: yerinde dönerek rakip ara
      motor(170, -170);
      break;
  }
  delay(5);
}
