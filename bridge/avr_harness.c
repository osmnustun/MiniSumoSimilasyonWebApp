/*
 * avr_harness.c — Mini Sumo AVR köprüsü
 *
 * Derlenmiş Arduino Nano (ATmega328P) firmware'ini simavr ile GERÇEK AVR
 * olarak çalıştırır ve giriş/çıkış pinlerini stdin/stdout üzerinden Python
 * fizik motoruna bağlar.
 *
 * Protokol (her fizik adımında bir paket):
 *   Python -> AVR (12 bayt, little-endian):
 *     uint16 qtr_left    (0..1023, ADC0 / A0)
 *     uint16 qtr_right   (0..1023, ADC1 / A1)
 *     uint16 vbat_mv     (0..5000, ADC5 / A5, opsiyonel)
 *     uint8  e3z_left    (0/1, D2)
 *     uint8  e3z_right   (0/1, D3)
 *     uint8  dip         (bit0..2 -> DIP switch, A2/A3/A4)
 *     uint8  btn         (0/1, D11 başlat butonu)
 *     uint8  reserved
 *   AVR -> Python (8 bayt, little-endian):
 *     uint16 pwm_left    (0..1023 duty, D9 / PB1 / OC1A)
 *     uint8  dir_left    (0/1, D7/D8)
 *     uint16 pwm_right   (0..1023 duty, D10 / PB2 / OC1B)
 *     uint8  dir_right   (0/1, D4/D12)
 *     uint8  enable_out  (0/1, D6)
 *     uint8  reserved
 *
 * Kullanım: avr_harness <firmware.elf> <freq_hz> <cycles_per_step>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>

#include "simavr/sim_avr.h"
#include "simavr/sim_elf.h"
#include "simavr/avr_ioport.h"
#include "simavr/avr_adc.h"
#include "simavr/sim_irq.h"

static avr_t *avr = NULL;

/* --- PWM duty ölçümü için pin izleme --- */
typedef struct {
    avr_irq_t *irq;
    uint64_t   last_cycle;
    uint32_t   last_value;
    uint64_t   high_cycles;
    uint32_t   value;       /* son değer */
} pin_tracker_t;

static void pin_notify(struct avr_irq_t *irq, uint32_t value, void *param)
{
    pin_tracker_t *t = (pin_tracker_t *)param;
    uint64_t now = avr->cycle;
    t->high_cycles += (now - t->last_cycle) * (t->last_value ? 1 : 0);
    t->last_cycle = now;
    t->last_value = value ? 1 : 0;
    t->value = value ? 1 : 0;
}

static void tracker_reset(pin_tracker_t *t)
{
    t->high_cycles = 0;
    t->last_cycle = avr->cycle;
    t->last_value = t->value;
}

static double tracker_duty(pin_tracker_t *t, uint64_t window_cycles)
{
    uint64_t now = avr->cycle;
    uint64_t high = t->high_cycles + (now - t->last_cycle) * (t->last_value ? 1 : 0);
    if (window_cycles == 0) return 0.0;
    double d = (double)high / (double)window_cycles;
    return d < 0 ? 0 : (d > 1 ? 1 : d);
}

static int read_full(int fd, void *buf, size_t n)
{
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, (char *)buf + got, n - got);
        if (r <= 0) return 0;
        got += (size_t)r;
    }
    return 1;
}

static int write_full(int fd, const void *buf, size_t n)
{
    size_t put = 0;
    while (put < n) {
        ssize_t w = write(fd, (const char *)buf + put, n - put);
        if (w <= 0) return 0;
        put += (size_t)w;
    }
    return 1;
}

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s <firmware.elf> [freq_hz] [cycles_per_step]\n", argv[0]);
        return 1;
    }

    const char *elf_path = argv[1];
    uint32_t freq = (argc > 2) ? (uint32_t)strtoul(argv[2], NULL, 10) : 16000000;
    uint64_t cycles_per_step = (argc > 3) ? strtoull(argv[3], NULL, 10) : (freq / 240);

    elf_firmware_t firmware;
    memset(&firmware, 0, sizeof(firmware));
    if (elf_read_firmware(elf_path, &firmware) != 0) {
        fprintf(stderr, "harness: firmware okunamadi: %s\n", elf_path);
        return 2;
    }
    firmware.frequency = freq;
    if (firmware.mmcu[0] == 0)
        strcpy(firmware.mmcu, "atmega328p");

    avr = avr_make_mcu_by_name("atmega328p");
    if (!avr) {
        fprintf(stderr, "harness: atmega328p olusturulamadi\n");
        return 3;
    }
    avr_init(avr);
    avr_load_firmware(avr, &firmware);
    avr->frequency = freq;

    /* --- PWM çıkış pinleri (Arduino D9=PB1, D10=PB2) --- */
    pin_tracker_t pwm_left = {0}, pwm_right = {0};
    pwm_left.irq  = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('B'), 1);
    pwm_right.irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('B'), 2);
    avr_irq_register_notify(pwm_left.irq, pin_notify, &pwm_left);
    avr_irq_register_notify(pwm_right.irq, pin_notify, &pwm_right);

    /* --- Dijital giriş pinleri --- */
    avr_irq_t *e3z_left_irq  = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), 2); /* D2 */
    avr_irq_t *e3z_right_irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), 3); /* D3 */
    avr_irq_t *dip1_irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('C'), 2); /* A2 */
    avr_irq_t *dip2_irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('C'), 3); /* A3 */
    avr_irq_t *dip3_irq = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('C'), 4); /* A4 */
    avr_irq_t *btn_irq  = avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('B'), 3); /* D11 */

    /* --- ADC kanalları (A0=ADC0, A1=ADC1, A5=ADC5) --- */
    avr_irq_t *adc0 = avr_io_getirq(avr, AVR_IOCTL_ADC_GETIRQ, 0);
    avr_irq_t *adc1 = avr_io_getirq(avr, AVR_IOCTL_ADC_GETIRQ, 1);
    avr_irq_t *adc5 = avr_io_getirq(avr, AVR_IOCTL_ADC_GETIRQ, 5);

    uint64_t next_step = avr->cycle + cycles_per_step;

    for (;;) {
        uint8_t in[12];
        if (!read_full(0, in, sizeof(in)))
            break; /* Python tarafi kapandi */

        uint16_t qtr_left  = (uint16_t)(in[0] | (in[1] << 8));
        uint16_t qtr_right = (uint16_t)(in[2] | (in[3] << 8));
        uint16_t vbat_mv   = (uint16_t)(in[4] | (in[5] << 8));
        uint8_t  e3z_left  = in[6];
        uint8_t  e3z_right = in[7];
        uint8_t  dip       = in[8];
        uint8_t  btn       = in[9];

        /* ADC: 0..1023 -> 0..5000 mV */
        avr_raise_irq(adc0, (uint32_t)((uint32_t)qtr_left  * 5000u / 1023u));
        avr_raise_irq(adc1, (uint32_t)((uint32_t)qtr_right * 5000u / 1023u));
        avr_raise_irq(adc5, vbat_mv);

        /* Dijital giriş: aktif-LOW değil, ham mantık seviyesi */
        avr_raise_irq(e3z_left_irq,  e3z_left  ? 1 : 0);
        avr_raise_irq(e3z_right_irq, e3z_right ? 1 : 0);
        /* DIP switch: INPUT_PULLUP -> kapalıyken LOW (0), açıkken HIGH (1) */
        avr_raise_irq(dip1_irq, (dip & 0x1) ? 0 : 1);
        avr_raise_irq(dip2_irq, (dip & 0x2) ? 0 : 1);
        avr_raise_irq(dip3_irq, (dip & 0x4) ? 0 : 1);
        avr_raise_irq(btn_irq, btn ? 0 : 1);

        /* PWM ölçüm penceresi başlat */
        tracker_reset(&pwm_left);
        tracker_reset(&pwm_right);
        uint64_t step_start = avr->cycle;
        next_step = step_start + cycles_per_step;

        /* AVR'yi bir adım boyunca çalıştır */
        while (avr->cycle < next_step) {
            int state = avr_run(avr);
            if (state == cpu_Done || state == cpu_Crashed)
                break;
        }

        uint64_t window = avr->cycle - step_start;
        (void)window;

        /* PWM: Arduino analogWrite() Timer1'i 8-bit faz-doğru PWM moduna alır
         * ve OCR1A (pin 9) / OCR1B (pin 10) yazmacına değeri yükler.
         * Duty = OCR / 255. OCR 0 iken pin sabit HIGH olabilir; bunu da ele al. */
        uint16_t ocr1a = avr->data[0x88];
        uint16_t ocr1b = avr->data[0x8A];
        uint16_t pwm_l = (uint16_t)((uint32_t)ocr1a * 1023u / 255u);
        uint16_t pwm_r = (uint16_t)((uint32_t)ocr1b * 1023u / 255u);
        if (ocr1a == 0 && pwm_left.value)
            pwm_l = 1023;
        if (ocr1b == 0 && pwm_right.value)
            pwm_r = 1023;

        /* Yön pinlerini PORT register'larindan oku:
         * H-kopru A/B mantigi: A=1,B=0 -> ileri; A=0,B=1 -> geri
         * Sol: A=D7(PD7), B=D8(PB0)   Sag: A=D4(PD4), B=D12(PB4)
         * Enable: D6(PD6) */
        uint8_t portb = avr->data[0x25];
        uint8_t portd = avr->data[0x2B];
        uint8_t d7  = (portd >> 7) & 1;   /* sol A  */
        uint8_t d8  = (portb >> 0) & 1;   /* sol B  */
        uint8_t d4  = (portd >> 4) & 1;   /* sag A  */
        uint8_t d12 = (portb >> 4) & 1;   /* sag B  */
        uint8_t dir_left  = (d7  && !d8)  ? 1 : 0;
        uint8_t dir_right = (d4  && !d12) ? 1 : 0;
        uint8_t enable_out = (portd >> 6) & 1;

        uint8_t out[8];
        out[0] = pwm_l & 0xFF; out[1] = (pwm_l >> 8) & 0xFF;
        out[2] = dir_left;
        out[3] = pwm_r & 0xFF; out[4] = (pwm_r >> 8) & 0xFF;
        out[5] = dir_right;
        out[6] = enable_out;
        out[7] = 0;
        if (!write_full(1, out, sizeof(out)))
            break;
    }

    return 0;
}
