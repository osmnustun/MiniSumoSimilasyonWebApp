# bridge/firmware.py
"""
Arduino Nano firmware derleme ve AVR emülasyon köprüsü.

Öğrencilerin yazdığı .ino kodu arduino-cli ile gerçek AVR makine koduna
derlenir ve avr_harness (libsimavr) ile gerçek bir ATmega328P gibi çalıştırılır.
"""
import os
import struct
import subprocess
import tempfile
import shutil
import threading

FQBN = "arduino:avr:nano"
FREQ = 16000000  # 16 MHz Arduino Nano

_HARNESS = shutil.which("avr_harness") or "/usr/local/bin/avr_harness"


class FirmwareError(Exception):
    pass


def compile_sketch(ino_source: str, sketch_name: str = "sumo_bot") -> str:
    """
    .ino kaynağını derler ve ELF dosyasının yolunu döndürür.

    Raises:
        FirmwareError: derleme başarısız olursa (hata çıktısı ile birlikte).
    """
    workdir = tempfile.mkdtemp(prefix="sumo_build_")
    sketch_dir = os.path.join(workdir, sketch_name)
    os.makedirs(sketch_dir, exist_ok=True)
    ino_path = os.path.join(sketch_dir, f"{sketch_name}.ino")
    with open(ino_path, "w") as f:
        f.write(ino_source)

    build_dir = os.path.join(workdir, "build")
    os.makedirs(build_dir, exist_ok=True)

    cmd = [
        "arduino-cli", "compile",
        "--fqbn", FQBN,
        "--output-dir", build_dir,
        sketch_dir,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        raise FirmwareError("Derleme zaman aşımına uğradı (180 sn).")

    if proc.returncode != 0:
        raise FirmwareError(proc.stderr or proc.stdout)

    elf_path = os.path.join(build_dir, f"{sketch_name}.ino.elf")
    if not os.path.exists(elf_path):
        # Bazı sürümlerde isim farklı olabilir; .elf ara
        for fn in os.listdir(build_dir):
            if fn.endswith(".elf"):
                elf_path = os.path.join(build_dir, fn)
                break
        else:
            raise FirmwareError("Derleme başarılı ama ELF bulunamadı.")

    return elf_path


class AVRFirmware:
    """
    avr_harness alt sürecini yönetir ve Python <-> AVR paket alışverişini yapar.
    """

    def __init__(self, elf_path: str, dt: float = 1.0 / 240.0):
        self.elf_path = elf_path
        self.cycles_per_step = max(1, int(FREQ * dt))
        self.proc = None
        self.lock = threading.Lock()
        # Son bilinen çıkış değerleri
        self.pwm_left = 0
        self.pwm_right = 0
        self.dir_left = 0
        self.dir_right = 0
        self.enable = 0
        self.alive = False

    def start(self):
        if not os.path.exists(_HARNESS):
            raise FirmwareError(f"avr_harness bulunamadı: {_HARNESS}")
        self.proc = subprocess.Popen(
            [_HARNESS, self.elf_path, str(FREQ), str(self.cycles_per_step)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.alive = True

    def step(self, qtr_left, qtr_right, e3z_left, e3z_right,
             dip=0, btn=1, vbat_mv=11100):
        """
        Bir fizik adımı boyunca AVR'yi çalıştırır ve motor çıkışlarını döndürür.
        """
        if not self.alive or self.proc is None:
            return None
        qtr_left = max(0, min(1023, int(qtr_left)))
        qtr_right = max(0, min(1023, int(qtr_right)))
        vbat_mv = max(0, min(5000, int(vbat_mv)))
        in_pkt = struct.pack(
            "<HHHBBBBBB",
            qtr_left, qtr_right, vbat_mv,
            int(bool(e3z_left)), int(bool(e3z_right)),
            int(dip) & 0x7, int(bool(btn)), 0, 0,
        )
        try:
            self.proc.stdin.write(in_pkt)
            self.proc.stdin.flush()
            out = self.proc.stdout.read(8)
            if len(out) != 8:
                self.alive = False
                return None
        except (BrokenPipeError, OSError):
            self.alive = False
            return None

        pwm_l, dir_l, pwm_r, dir_r, enable, _ = struct.unpack("<HBHBBB", out)
        self.pwm_left = pwm_l
        self.pwm_right = pwm_r
        self.dir_left = dir_l
        self.dir_right = dir_r
        self.enable = enable
        return {
            "pwm_left": pwm_l,
            "pwm_right": pwm_r,
            "dir_left": dir_l,
            "dir_right": dir_r,
            "enable": enable,
        }

    def stop(self):
        self.alive = False
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
