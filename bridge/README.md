# bridge/README.md
# AVR Köprüsü (avr_harness)

`avr_harness.c`, `libsimavr` kullanarak derlenmiş Arduino Nano (ATmega328P)
firmware'ini **gerçek AVR** olarak çalıştırır ve pin giriş/çıkışlarını
stdin/stdout üzerinden Python fizik motoruna bağlar.

Docker imajı derlenirken otomatik olarak `/usr/local/bin/avr_harness` konumuna
kurulur.

## Protokol

Her fizik adımında bir paket alışverişi yapılır:

**Python → AVR (12 bayt, little-endian)**
| Alan | Tip | Açıklama |
|---|---|---|
| qtr_left | uint16 | Sol QTR-1A (0–1023) → ADC0 (A0) |
| qtr_right | uint16 | Sağ QTR-1A (0–1023) → ADC1 (A1) |
| vbat_mv | uint16 | Pil gerilimi mV → ADC5 (A5) |
| e3z_left | uint8 | Sol E3Z-D81 (0/1) → D2 |
| e3z_right | uint8 | Sağ E3Z-D81 (0/1) → D3 |
| dip | uint8 | DIP switch bitmask (bit0→A2, bit1→A3, bit2→A4) |
| btn | uint8 | Başlat butonu → D11 |
| (reserved) | uint8 ×2 | — |

**AVR → Python (8 bayt, little-endian)**
| Alan | Tip | Açıklama |
|---|---|---|
| pwm_left | uint16 | Sol motor PWM (0–1023), OCR1A (D9) |
| dir_left | uint8 | Sol yön (D7/D8) |
| pwm_right | uint16 | Sağ motor PWM (0–1023), OCR1B (D10) |
| dir_right | uint8 | Sağ yön (D4/D12) |
| enable | uint8 | Motor enable (D6) |
| (reserved) | uint8 | — |

## Manuel Derleme

```bash
gcc -O2 -o avr_harness avr_harness.c -I/usr/include/simavr -lsimavr -lm
```

## Kullanım

```bash
avr_harness <firmware.elf> [freq_hz] [cycles_per_step]
# örn: avr_harness build/sketch.ino.elf 16000000 66666
```
