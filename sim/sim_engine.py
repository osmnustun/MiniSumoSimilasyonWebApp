# sim/sim_engine.py
"""
Mini Sumo Simülasyon Motoru

PyBullet 3D dünyasını arka plan thread'inde çalıştırır, iki robotu AVR
firmware'leri ile kontrol eder, dohyo kenarını ve rakip sensörlerini hesaplar,
ve web arayüzü için kare (JPEG) üretir.
"""
import os
import time
import math
import threading
import numpy as np

import pybullet as p

from bridge.firmware import AVRFirmware

# --- Dohyo / robot sabitleri ---
RING_DIAMETER = 0.770          # 77 cm
RING_RADIUS = RING_DIAMETER / 2
EDGE_WIDTH = 0.025             # 2.5 cm siyah kenar
ROBOT_START_X = 0.10           # başlangıç çizgileri merkeze göre (resmi: ~10 cm)
E3Z_RANGE = 0.30               # E3Z-D81 algılama mesafesi (m)
WHEEL_RADIUS = 0.021           # URDF'teki tekerlek yarıçapı

# --- Motor modeli (jSumo Force Up 6V, 250 RPM) ---
# Gerçek motor: durma torku ~0.6 kg·cm, yüksüz ~250 RPM. Tekerlek bir hız
# kaynağı olarak sürülür (PWM ∝ hedef hız) → gerçek DC motorun
# τ ∝ (ω_komut − ω) davranışı. Aktarılan kuvvet zemin tutunmasıyla sınırlanır.
MOTOR_TORQUE_STALL = 0.060     # N·m — durma torku (~0.6 kg·cm)
MOTOR_FORCE_STALL = MOTOR_TORQUE_STALL / WHEEL_RADIUS   # ≈ 2.86 N / tekerlek
MOTOR_MAX_RAD_S = 26.0         # yüksüz açısal hız (250 RPM) → ~0.55 m/s
MOTOR_FORCE_ARG = 0.10         # PyBullet motor kuvvet argümanı (≈ durma kuvveti)

# Tekerlek lastiği sürtünmesi (dohyo zemininde gerçekçi tutunma).
# İtme kuvveti bu tutunmayla sınırlanır → ağır robot daha sert iter.
WHEEL_FRICTION = 1.2

# URDF'te base_link çerçevesi = ağırlık merkezi (CoM) ve CoM tekerlek aksı
# hizasındadır. Bu yüzden robotun doğduğu z = aks yüksekliği.
ROBOT_SPAWN_Z = 0.0207
MOTOR_MAX_RAD_S = 26.0         # 250 RPM → ~0.55 m/s

# --- Temas / çarpışma modeli ---
SOLVER_ITERATIONS = 120        # Bullet varsayılanı 50; hızlı itişmelerde kararlılık
CONTACT_BREAKING_THRESHOLD = 0.0015
CONTACT_LOSS = 0.06            # çarpışmada sönümlenen yaklaşma hızı oranı (plastik kayıp)
CONTACT_MIN_SPEED = 0.03       # m/s — bunun altındaki yaklaşmalar "temas" sayılır

URDF_PATH = os.path.join(os.path.dirname(__file__), "urdf", "mini_sumo_bot.urdf")


class RobotState:
    def __init__(self, name, color, x, y, yaw):
        self.name = name
        self.color = color
        self.x = x
        self.y = y
        self.z = ROBOT_SPAWN_Z
        self.yaw = yaw
        self.quat = [0, 0, 0, 1]
        self.wheel_l = 0.0
        self.wheel_r = 0.0
        self.body_id = None
        self.mass_g = 500
        # Kütle/atalet muhasebesi (toplam kütle = paneldeki değer)
        self.link_extra_mass = 0.0     # gövde dışı linklerin toplam kütlesi
        self.base_mass0 = 0.5          # URDF'teki gövde kütlesi
        self.base_inertia = None       # URDF gövde atalet diyagonali
        self.contact = 0.0             # rakiple temas kuvveti (N)
        self.joint_left = None
        self.joint_right = None
        self.firmware: AVRFirmware = None
        self.pwm_left = 0
        self.pwm_right = 0
        self.sensors = {"qtr_left": 0, "qtr_right": 0,
                        "e3z_left": 0, "e3z_right": 0}
        self.ring_out = False

    def to_dict(self):
        return {
            "name": self.name,
            "color": self.color,
            "position": [self.x, self.y],
            "pos3": [self.x, self.y, self.z],
            "quat": list(self.quat),
            "yaw": self.yaw,
            "wheel_l": self.wheel_l,
            "wheel_r": self.wheel_r,
            "mass_g": self.mass_g,
            "pwm_left": self.pwm_left,
            "pwm_right": self.pwm_right,
            "sensors": dict(self.sensors),
            "ring_out": self.ring_out,
            "contact": round(self.contact, 2),
        }


class SimulationEngine:
    def __init__(self, render_width=800, render_height=533):
        self.lock = threading.Lock()
        self.running = False
        self.thread = None

        self.width = render_width
        self.height = render_height
        self.latest_frame = None      # PNG/JPEG bytes
        self.frame_seq = 0

        self.time = 0.0
        self.dt = 1.0 / 240.0
        self.status = "idle"          # idle | countdown | running | finished
        self.winner = None
        self.countdown = 0.0
        self.match_duration = 180.0
        # Tur / maç skoru (best-of-3)
        self.best_of = 3
        self.wins_needed = 2          # 3 turdan 2'sini alan kazanır
        self.round = 0
        self.score = [0, 0]
        self.round_winner = None
        self.match_winner = None
        # Tur arası: DIP switch'lerin değiştirilebildiği pencere
        self.break_duration = 12.0
        self.break_left = 0.0
        self.dips = [0, 0]            # robot başına DIP switch bitmask (3 switch)
        self.start_button = 1         # aktif-LOW firmware tarafinda

        self.robots = []
        self.client = None
        self.floor_id = None
        self.target_id = None          # E3Z testi için hedef nesne
        self.target_pos = [0.15, 0.0]

        self.mode = "idle"             # idle | match | test
        self.manual = {"left": 0, "right": 0}   # manuel motor komutu (PWM -1023..1023)
        self.test_has_firmware = False
        self._pending_place = None     # (x, y, yaw)
        self._pending_target = None    # (x, y)
        self._pending_round_reset = False

        self._pending_compile = []    # (index, ino_source)
        self.last_error = None

    # ------------------------------------------------------------------
    # Kurulum
    # ------------------------------------------------------------------
    def _connect(self):
        if self.client is None:
            self.client = p.connect(p.DIRECT)
            p.setGravity(0, 0, -9.81)
            p.setTimeStep(self.dt)
            self._configure_physics()
            self._build_world()

    def _configure_physics(self):
        """Temas çözümünü gerçekçileştiren Bullet parametreleri.

        * Yüksek çözücü iterasyonu → hızlı itişmelerde kararlı, az sızan temas
        * Koni (cone) sürtünmesi → lastik benzeri, yanal/boyuna sürtünme kuplajı
        * Split impulse → nüfuz (penetration) düzeltmesi hıza sızmaz
        """
        for kw in (
            dict(fixedTimeStep=self.dt),
            dict(numSolverIterations=SOLVER_ITERATIONS),
            dict(contactBreakingThreshold=CONTACT_BREAKING_THRESHOLD),
            dict(enableConeFriction=1),
            dict(useSplitImpulse=1, splitImpulsePenetrationThreshold=-0.002),
            dict(deterministicOverlappingPairs=1),
        ):
            try:
                p.setPhysicsEngineParameter(**kw)
            except Exception:
                pass  # eski PyBullet sürümlerinde bazı anahtarlar yok

    def _build_world(self):
        # Zemin: siyah taban (tam cap) + beyaz ust kaplama (kenar bosluğu kadar küçük)
        black_col = p.createCollisionShape(p.GEOM_CYLINDER, radius=RING_RADIUS, height=0.02)
        black_vis = p.createVisualShape(p.GEOM_CYLINDER, radius=RING_RADIUS, length=0.02,
                                        rgbaColor=[0.04, 0.04, 0.04, 1])
        self.floor_id = p.createMultiBody(0, black_col, black_vis, basePosition=[0, 0, -0.01])
        p.changeDynamics(self.floor_id, -1, lateralFriction=0.9)

        white_r = RING_RADIUS - EDGE_WIDTH
        white_vis = p.createVisualShape(p.GEOM_CYLINDER, radius=white_r, length=0.02,
                                        rgbaColor=[0.97, 0.97, 0.97, 1])
        self.white_id = p.createMultiBody(0, -1, white_vis, basePosition=[0, 0, -0.009])

    def _create_target(self, x=0.15, y=0.0):
        # E3Z sensör testi için yerleştirilebilir hedef (rakip benzeri nesne)
        self.target_pos = [x, y]
        col = p.createCollisionShape(p.GEOM_BOX, halfExtents=[0.045, 0.035, 0.0125])
        vis = p.createVisualShape(p.GEOM_BOX, halfExtents=[0.045, 0.035, 0.0125],
                                  rgbaColor=[0.9, 0.2, 0.2, 1])
        self.target_id = p.createMultiBody(1.0, col, vis, basePosition=[x, y, 0.015])
        p.changeDynamics(self.target_id, -1, lateralFriction=0.7)

    def _set_target_pos(self, x, y):
        self.target_pos = [x, y]
        if self.target_id is not None:
            p.resetBasePositionAndOrientation(self.target_id, [x, y, 0.015], [0, 0, 0, 1])

    def _load_robot(self, robot: RobotState):
        # Renk icin URDF'i gecici olarak ozelleştir
        with open(URDF_PATH, "r") as f:
            xml = f.read()
        c = robot.color
        xml = xml.replace(
            '<material name="top"><color rgba="0.10 0.55 0.95 1.0"/></material>',
            f'<material name="top"><color rgba="{c[0]} {c[1]} {c[2]} 1.0"/></material>',
        )
        import tempfile
        tmp = tempfile.NamedTemporaryFile("w", suffix=".urdf", delete=False)
        tmp.write(xml)
        tmp.close()

        robot.body_id = p.loadURDF(
            tmp.name,
            basePosition=[robot.x, robot.y, ROBOT_SPAWN_Z],
            baseOrientation=p.getQuaternionFromEuler([0, 0, robot.yaw]),
            useFixedBase=False,
        )

        # --- Kütle/atalet referansları (URDF değerleri) ---
        d0 = p.getDynamicsInfo(robot.body_id, -1)
        robot.base_mass0 = d0[0] or 0.5
        robot.base_inertia = list(d0[2])
        # Gövde dışı linklerin (tekerlek, kızak, kama, sensör) toplam kütlesi;
        # toplam kütle panelde ayarlanan değere tam eşitlenir.
        robot.link_extra_mass = sum(
            p.getDynamicsInfo(robot.body_id, j)[0]
            for j in range(p.getNumJoints(robot.body_id))
        )
        self._apply_mass(robot)

        # --- Gövde: PCB/plastik; neredeyse tamamen sönümlü çarpışma ---
        p.changeDynamics(robot.body_id, -1, lateralFriction=0.62, restitution=0.03,
                         rollingFriction=0.0, spinningFriction=0.0)

        # --- Link bazlı malzeme: tekerlek (çekiş), kızak (yönlü), kama (kaygan) ---
        for j in range(p.getNumJoints(robot.body_id)):
            name = p.getJointInfo(robot.body_id, j)[12].decode()
            if name == "skid":
                # Yönlü kızak: ileri kolay kayar (0.25×), yana direnç (1.7×)
                p.changeDynamics(robot.body_id, j, lateralFriction=0.06,
                                 anisotropicFriction=[0.25, 1.7, 1.7],
                                 spinningFriction=0.0, rollingFriction=0.0,
                                 restitution=0.0)
            elif name == "blade":
                # Cilalı çelik kama: rakibin altına kayar, sürtünmesi çok düşük.
                # Anizotropik: rampa yönünde (x) daha da kaygan → kaldırma kolay.
                p.changeDynamics(robot.body_id, j, lateralFriction=0.10,
                                 anisotropicFriction=[0.55, 1.0, 1.0],
                                 restitution=0.02, rollingFriction=0.0,
                                 spinningFriction=0.0)
            elif name == "left_wheel":
                robot.joint_left = j
                p.changeDynamics(robot.body_id, j, lateralFriction=WHEEL_FRICTION,
                                 rollingFriction=0.0015, spinningFriction=0.01)
            elif name == "right_wheel":
                robot.joint_right = j
                p.changeDynamics(robot.body_id, j, lateralFriction=WHEEL_FRICTION,
                                 rollingFriction=0.0015, spinningFriction=0.01)
        for j in (robot.joint_left, robot.joint_right):
            if j is not None:
                p.setJointMotorControl2(robot.body_id, j, p.VELOCITY_CONTROL, 0, 0)

    def _apply_mass(self, robot: RobotState):
        """Toplam kütleyi paneldeki değere eşitle; ataleti orantılı ölçekle.

        Ağır robot yalnızca daha zor itilmez, aynı zamanda dönmeye karşı da
        daha dirençli olur (atalet kütleyle birlikte ölçeklenir).
        """
        total = robot.mass_g / 1000.0
        base = max(0.05, total - robot.link_extra_mass)
        scale = (base / robot.base_mass0) if robot.base_mass0 else 1.0
        kwargs = {"mass": base}
        if robot.base_inertia:
            kwargs["localInertiaDiagonal"] = [max(1e-7, d * scale)
                                              for d in robot.base_inertia]
        p.changeDynamics(robot.body_id, -1, **kwargs)

    def set_weight(self, index, mass_g):
        with self.lock:
            if self._weight_locked():
                return False, "Ağırlık tur sırasında kilitli; turlar arasında değiştirebilirsiniz."
            if 0 <= index < len(self.robots):
                self.robots[index].mass_g = mass_g
                if self.robots[index].body_id is not None:
                    self._apply_mass(self.robots[index])
                return True, None
            return False, "Robot bulunamadı."

    def set_all_weights(self, mass_g):
        for i in range(len(self.robots)):
            ok, err = self.set_weight(i, mass_g)
            if not ok:
                return ok, err
        return True, None

    # --- kilit kuralları -------------------------------------------------
    def _dip_locked(self):
        """DIP switch'ler yalnızca maç aralarında / turlar arasında açıktır."""
        return self.mode == "match" and self.status in ("countdown", "running")

    def _weight_locked(self):
        return self.mode == "match" and self.status in ("countdown", "running")

    @property
    def dip_locked(self):
        return self._dip_locked()

    @property
    def weight_locked(self):
        return self._weight_locked()

    # ------------------------------------------------------------------
    # Baslatma / durdurma
    # ------------------------------------------------------------------
    def start(self, fw1_source, fw2_source, duration=180.0, break_duration=12.0):
        """
        fw1_source / fw2_source: .ino kaynak metni
        break_duration: turlar arasında DIP değiştirme penceresi (sn)
        """
        self.stop(join=True)
        with self.lock:
            self.last_error = None
            self.match_duration = duration
            self.break_duration = max(1.0, float(break_duration))
            self.break_left = 0.0
            self.time = 0.0
            self.winner = None
            self.status = "countdown"
            self.countdown = 5.0
            self.round = 1
            self.score = [0, 0]
            self.round_winner = None
            self.match_winner = None
            self.mode = "match"
        self.running = True
        self.thread = threading.Thread(
            target=self._run, args=(fw1_source, fw2_source), daemon=True
        )
        self.thread.start()

    def stop(self, join=False):
        self.running = False
        if join and self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)

    def reset(self):
        self.stop(join=True)
        with self.lock:
            self.status = "idle"
            self.mode = "idle"
            self.time = 0.0
            self.winner = None
            self.break_left = 0.0
            self.robots = []
            self.latest_frame = None
            self.target_id = None
        if self.client is not None:
            p.resetSimulation()
            self.client = None

    # ------------------------------------------------------------------
    # TEST / EĞİTİM MODU
    # ------------------------------------------------------------------
    def start_test(self):
        """Tek robotun manuel kontrol edildiği test/eğitim modunu başlat."""
        self._launch_test(None)

    def start_test_firmware(self, code):
        """Test/eğitim modunda tek robotu derlenen firmware ile çalıştır."""
        from bridge.firmware import compile_sketch
        elf = compile_sketch(code, "sumo_test")   # hata fırlatabilir
        self._launch_test(elf)

    def _launch_test(self, elf_path):
        self.stop(join=True)
        with self.lock:
            self.mode = "test"
            self.status = "test"
            self.last_error = None
            self.time = 0.0
            self.break_left = 0.0
            self.manual = {"left": 0, "right": 0}
            self.test_has_firmware = elf_path is not None
        self.running = True
        self.thread = threading.Thread(target=self._run_test, args=(elf_path,), daemon=True)
        self.thread.start()

    def set_manual_motors(self, left, right):
        """Manuel motor komutu (PWM -1023..1023)."""
        with self.lock:
            self.manual = {"left": int(left), "right": int(right)}

    def place_robot(self, x, y, yaw=0.0):
        """Robotun konumunu anında değiştir (sensör testi için)."""
        with self.lock:
            self._pending_place = (x, y, yaw)

    def set_target(self, x, y):
        with self.lock:
            self._pending_target = (x, y)

    def _run_test(self, elf_path=None):
        self._connect()
        p.resetSimulation()
        p.setGravity(0, 0, -9.81)
        p.setTimeStep(self.dt)
        self._build_world()
        self._create_target()

        robot = RobotState("Test Robotu", [0.2, 0.5, 1.0], 0.0, 0.0, 0.0)
        self._load_robot(robot)
        if elf_path:
            robot.firmware = AVRFirmware(elf_path, self.dt)
            robot.firmware.start()
        with self.lock:
            self.robots = [robot]
            self.status = "test"

        render_counter = 0
        while self.running:
            loop_start = time.time()

            with self.lock:
                manual = dict(self.manual)
                dips = list(self.dips)
                place = self._pending_place
                self._pending_place = None
                target = self._pending_target
                self._pending_target = None

            if place is not None:
                p.resetBasePositionAndOrientation(
                    robot.body_id, [place[0], place[1], ROBOT_SPAWN_Z],
                    p.getQuaternionFromEuler([0, 0, place[2]]))
                p.resetBaseVelocity(robot.body_id, [0, 0, 0], [0, 0, 0])
            if target is not None:
                self._set_target_pos(*target)

            self._update_robot_pose(robot)
            self._compute_sensors(robot)

            if elf_path and robot.firmware is not None:
                s = robot.sensors
                out = robot.firmware.step(
                    s["qtr_left"], s["qtr_right"],
                    s["e3z_left"], s["e3z_right"], dip=dips[0], btn=1)
                if out is None:
                    self._set_motors(robot, 0, 0)
                else:
                    robot.pwm_left = out["pwm_left"]
                    robot.pwm_right = out["pwm_right"]
                    lv = (out["pwm_left"] / 1023.0) * MOTOR_MAX_RAD_S
                    rv = (out["pwm_right"] / 1023.0) * MOTOR_MAX_RAD_S
                    if out["dir_left"] == 0:
                        lv = -lv
                    if out["dir_right"] == 0:
                        rv = -rv
                    self._set_motors(robot, lv, rv)
            else:
                lv = (manual["left"] / 1023.0) * MOTOR_MAX_RAD_S
                rv = (manual["right"] / 1023.0) * MOTOR_MAX_RAD_S
                robot.pwm_left = abs(manual["left"])
                robot.pwm_right = abs(manual["right"])
                self._set_motors(robot, lv, rv)

            p.stepSimulation()
            # Test hedefine çarpma kuvveti (eğitim modunda telemetri)
            robot.contact = self._contact_force(robot.body_id, self.target_id)

            render_counter += 1
            with self.lock:
                if render_counter >= 24:
                    render_counter = 0
                    do_render = True
                else:
                    do_render = False
            if do_render:
                self._render()

            elapsed = time.time() - loop_start
            sleep = self.dt - elapsed
            if sleep > 0:
                time.sleep(sleep)

        if robot.firmware:
            robot.firmware.stop()

    # ------------------------------------------------------------------
    # Ana dongu
    # ------------------------------------------------------------------
    def _run(self, fw1_source, fw2_source):
        try:
            # Firmware derle
            from bridge.firmware import compile_sketch
            elf1 = compile_sketch(fw1_source, "sumo_bot_1")
            elf2 = compile_sketch(fw2_source, "sumo_bot_2")
        except Exception as e:
            with self.lock:
                self.last_error = str(e)
                self.status = "error"
            self.running = False
            return

        self._connect()
        # Dunyayi taze baslat
        p.resetSimulation()
        p.setGravity(0, 0, -9.81)
        p.setTimeStep(self.dt)
        self._build_world()

        robots = [
            # Sırt sırta başlangıç (resmi mini sumo): her robot merkeze SIRTINI döner
            RobotState("Mavi Robot", [0.2, 0.5, 1.0], -ROBOT_START_X, 0, math.pi),
            RobotState("Kirmizi Robot", [0.9, 0.2, 0.2], ROBOT_START_X, 0, 0.0),
        ]
        for r in robots:
            self._load_robot(r)
        robots[0].firmware = AVRFirmware(elf1, self.dt)
        robots[1].firmware = AVRFirmware(elf2, self.dt)
        robots[0].firmware.start()
        robots[1].firmware.start()
        with self.lock:
            self.robots = robots

        render_counter = 0
        ticks = 0
        while self.running:
            loop_start = time.time()
            break_done = False

            # --- kisa kilit: paylasilan ayarlari oku ---
            with self.lock:
                status = self.status
                dips = list(self.dips)
                if status == "countdown":
                    self.countdown -= self.dt
                    if self.countdown <= 0:
                        self.status = "running"
                        status = "running"
                elif status == "break":
                    # Tur arası: DIP switch'ler açık, süre bitince yeni tur
                    self.break_left -= self.dt
                    if self.break_left <= 0:
                        self.break_left = 0.0
                        break_done = True

            # --- agir is: kilit YOK ---
            for r in robots:
                self._update_robot_pose(r)
            for r in robots:
                self._compute_sensors(r)

            active = (status == "running")
            for i, r in enumerate(robots):
                if not active or r.firmware is None:
                    self._set_motors(r, 0, 0)
                    continue
                s = r.sensors
                out = r.firmware.step(
                    s["qtr_left"], s["qtr_right"],
                    s["e3z_left"], s["e3z_right"],
                    dip=dips[i] if i < len(dips) else 0, btn=1,
                )
                if out is None:
                    self._set_motors(r, 0, 0)
                    continue
                r.pwm_left = out["pwm_left"]
                r.pwm_right = out["pwm_right"]
                lv = (out["pwm_left"] / 1023.0) * MOTOR_MAX_RAD_S
                rv = (out["pwm_right"] / 1023.0) * MOTOR_MAX_RAD_S
                if out["dir_left"] == 0:
                    lv = -lv
                if out["dir_right"] == 0:
                    rv = -rv
                self._set_motors(r, lv, rv)

            p.stepSimulation()
            ticks += 1
            # Çarpışma tepkisi: rakip temasını ölç + plastik kayıp uygula
            self._update_contacts(robots)

            # --- kisa kilit: durum guncelle ---
            with self.lock:
                if status == "running":
                    self.time += self.dt
                    self._check_ring_out()
                    if self.time >= self.match_duration and self.winner is None:
                        self._decide_by_position()
                    if self.winner is not None:
                        self._finish_round()

                render_counter += 1
                if render_counter >= 24:  # ~10 fps sunucu render (yedek; asıl görünüm WebGL)
                    render_counter = 0
                    do_render = True
                else:
                    do_render = False

            if break_done and self.running:
                self._reset_round(robots)

            if do_render:
                self._render()

            # Gercek zaman senkronu
            elapsed = time.time() - loop_start
            sleep = self.dt - elapsed
            if sleep > 0:
                time.sleep(sleep)

        # temizlik
        for r in robots:
            if r.firmware:
                r.firmware.stop()

    # ------------------------------------------------------------------
    # Yardimcilar
    # ------------------------------------------------------------------
    def _update_robot_pose(self, robot):
        pos, orn = p.getBasePositionAndOrientation(robot.body_id)
        robot.x, robot.y = pos[0], pos[1]
        robot.z = pos[2]
        robot.quat = orn
        e = p.getEulerFromQuaternion(orn)
        robot.yaw = e[2]
        # tekerlek açıları (görsel dönme için)
        try:
            if robot.joint_left is not None:
                robot.wheel_l = p.getJointState(robot.body_id, robot.joint_left)[0]
            if robot.joint_right is not None:
                robot.wheel_r = p.getJointState(robot.body_id, robot.joint_right)[0]
        except Exception:
            pass

    def _set_motors(self, robot, left_rad_s, right_rad_s):
        if robot.joint_left is not None:
            p.setJointMotorControl2(robot.body_id, robot.joint_left,
                                    p.VELOCITY_CONTROL, targetVelocity=left_rad_s,
                                    force=MOTOR_FORCE_ARG)
        if robot.joint_right is not None:
            p.setJointMotorControl2(robot.body_id, robot.joint_right,
                                    p.VELOCITY_CONTROL, targetVelocity=right_rad_s,
                                    force=MOTOR_FORCE_ARG)

    # ------------------------------------------------------------------
    # Çarpışma / temas modeli
    # ------------------------------------------------------------------
    def _contact_force(self, a_id, b_id):
        """İki cisim arasındaki toplam temas normal kuvveti (N)."""
        if a_id is None or b_id is None:
            return 0.0
        try:
            pts = p.getContactPoints(bodyA=a_id, bodyB=b_id)
        except Exception:
            return 0.0
        return float(sum(abs(cp[9]) for cp in pts))

    def _update_contacts(self, robots):
        """Rakip temasını ölç ve hafif *plastik* çarpışma kaybı uygula.

        Bullet teması ideal (neredeyse elastik) çözer. Gerçek şasi/bıçak
        çarpışmalarında enerjinin bir kısmı deformasyon, ses ve ısı olarak
        kaybolur. Yaklaşma hızının küçük bir oranını (CONTACT_LOSS) her
        iki robota zıt yönde impuls olarak uygulayarak bu kaybı modelleriz.
        Temas kuvveti telemetri için `robot.contact` alanına yazılır.
        """
        for r in robots:
            r.contact = 0.0
        if len(robots) != 2:
            return
        a, b = robots
        if a.body_id is None or b.body_id is None:
            return
        try:
            pts = p.getContactPoints(bodyA=a.body_id, bodyB=b.body_id)
        except Exception:
            return
        if not pts:
            return

        force = float(sum(abs(cp[9]) for cp in pts))
        a.contact = force
        b.contact = force

        n = pts[0][7]                       # temas normali (B'den A'ya)
        va = p.getBaseVelocity(a.body_id)[0]
        vb = p.getBaseVelocity(b.body_id)[0]
        vn = sum((vb[i] - va[i]) * n[i] for i in range(3))
        if vn <= CONTACT_MIN_SPEED:         # yaklaşma yok (birlikte itiliyorlar)
            return

        ma = max(a.mass_g / 1000.0, 1e-3)
        mb = max(b.mass_g / 1000.0, 1e-3)
        # Yaklaşma hızını CONTACT_LOSS oranında söndüren impuls
        j = CONTACT_LOSS * vn / (1.0 / ma + 1.0 / mb)
        for r, sign in ((a, 1.0), (b, -1.0)):
            f = [sign * j / self.dt * n[i] for i in range(3)]
            try:
                com = p.getDynamicsInfo(r.body_id, -1)[3]
                pos, orn = p.getBasePositionAndOrientation(r.body_id)
                com_world = p.multiplyTransforms(pos, orn, com, [0, 0, 0, 1])[0]
                # Kuvveti tam CoM'a uygula → sahte tork oluşmaz
                p.applyExternalForce(r.body_id, -1, f, com_world, p.WORLD_FRAME)
            except Exception:
                pass

    def _compute_sensors(self, robot):
        # QTR-1A: zemin rengi (beyaz=~950, siyah=~150)
        # Sensörler robotun ön-alt köşelerinde; konuma göre kenarı okurlar.
        cos_y = math.cos(robot.yaw)
        sin_y = math.sin(robot.yaw)
        offsets = [(0.028, 0.026), (0.028, -0.026)]  # (ileri, yan) sol/sağ — URDF ile aynı
        vals = []
        for (fx, fy) in offsets:
            wx = robot.x + fx * cos_y - fy * sin_y
            wy = robot.y + fx * sin_y + fy * cos_y
            d = math.hypot(wx, wy)
            on_edge = d > (RING_RADIUS - EDGE_WIDTH)
            vals.append(150 if on_edge else 950)
        robot.sensors["qtr_left"] = vals[0]
        robot.sensors["qtr_right"] = vals[1]

        # E3Z-D81: ön sensörlerin hedef/rakip görmesi
        targets = []
        for r in self.robots:
            if r is not robot and not r.ring_out:
                targets.append((r.x, r.y))
        if self.target_id is not None:
            targets.append((self.target_pos[0], self.target_pos[1]))

        fwd_x, fwd_y = cos_y, sin_y
        el = er = 0
        for (tx, ty) in targets:
            dx, dy = tx - robot.x, ty - robot.y
            dist = math.hypot(dx, dy)
            if dist < 1e-6:
                continue
            dot = dx * fwd_x + dy * fwd_y
            if dot <= 0 or dist > E3Z_RANGE:
                continue
            cross = dx * fwd_y - dy * fwd_x  # + ise sol, - ise sağ
            # iki sensör hafif açılı: ±yaklaşık 6 cm yanal penceresi
            if cross >= -0.06:
                el = 1
            if cross <= 0.06:
                er = 1
        robot.sensors["e3z_left"] = el
        robot.sensors["e3z_right"] = er

    def _check_ring_out(self):
        for i, r in enumerate(self.robots):
            dist = math.hypot(r.x, r.y)
            if dist > RING_RADIUS:  # merkez kenari gecti
                r.ring_out = True
                other = 1 - i
                if self.winner is None:
                    self.winner = other

    def _decide_by_position(self):
        d0 = math.hypot(self.robots[0].x, self.robots[0].y)
        d1 = math.hypot(self.robots[1].x, self.robots[1].y)
        self.winner = 0 if d0 <= d1 else 1

    def _finish_round(self):
        """Bir tur bitti: skoru işle, maç bittiyse bitir, değilse tura ara ver."""
        w = self.winner
        if w is None:
            return
        self.score[w] += 1
        self.round_winner = w
        if self.score[w] >= self.wins_needed:
            self.match_winner = w
            self.status = "finished"
            self.break_left = 0.0
        else:
            self.round += 1
            # DIP switch'lerin değiştirilebildiği tur arası penceresi
            self.status = "break"
            self.break_left = self.break_duration
            self.countdown = 5.0

    def _reset_round(self, robots):
        """Robotları başlangıç pozisyonlarına (sırt sırta) döndür."""
        poses = [(-ROBOT_START_X, 0.0, math.pi), (ROBOT_START_X, 0.0, 0.0)]
        for r, (x, y, yaw) in zip(robots, poses):
            if r.body_id is not None:
                p.resetBasePositionAndOrientation(
                    r.body_id, [x, y, ROBOT_SPAWN_Z],
                    p.getQuaternionFromEuler([0, 0, yaw]))
                p.resetBaseVelocity(r.body_id, [0, 0, 0], [0, 0, 0])
            r.ring_out = False
            r.pwm_left = 0
            r.pwm_right = 0
        with self.lock:
            self.time = 0.0
            self.winner = None
            self.break_left = 0.0
            self.status = "countdown"
            self.countdown = 5.0

    def _render(self):
        try:
            view = p.computeViewMatrixFromYawPitchRoll(
                cameraTargetPosition=[0, 0, 0.0],
                distance=1.15,
                yaw=45,
                pitch=-38,
                roll=0,
                upAxisIndex=2,
            )
            proj = p.computeProjectionMatrixFOV(
                fov=50, aspect=self.width / self.height, nearVal=0.05, farVal=100
            )
            _, _, rgb, _, _ = p.getCameraImage(
                self.width, self.height, view, proj,
                renderer=p.ER_TINY_RENDERER,
            )
            arr = np.reshape(rgb, (self.height, self.width, 4)).astype(np.uint8)
            img = arr[:, :, :3]
            from PIL import Image
            import io
            buf = io.BytesIO()
            Image.fromarray(img).save(buf, format="JPEG", quality=80)
            with self.lock:
                self.latest_frame = buf.getvalue()
                self.frame_seq += 1
        except Exception as e:
            self.last_error = f"render: {e}"

    def get_frame(self):
        with self.lock:
            return self.latest_frame

    def get_state(self):
        with self.lock:
            return {
                "status": self.status,
                "mode": self.mode,
                "time": round(self.time, 2),
                "duration": self.match_duration,
                "winner": self.winner,
                "round": self.round,
                "best_of": self.best_of,
                "score": list(self.score),
                "round_winner": self.round_winner,
                "match_winner": self.match_winner,
                "dip": self.dips[0] if self.dips else 0,
                "dips": list(self.dips),
                "dip_locked": self._dip_locked(),
                "weight_locked": self._weight_locked(),
                "break_left": round(self.break_left, 1),
                "break_duration": self.break_duration,
                "countdown": round(self.countdown, 1),
                "manual": dict(self.manual),
                "test_has_firmware": self.test_has_firmware,
                "target": list(self.target_pos) if self.target_id is not None else None,
                "error": self.last_error,
                "robots": [r.to_dict() for r in self.robots],
            }

    def set_dip(self, dip, robot=None):
        """Robot başına DIP switch (bitmask). robot=None ise hepsine uygula.

        Dönüş: (ok, hata_mesajı). DIP'ler tur sırasında (countdown/running)
        kilitlidir; yalnızca maç aralarında ve turlar arasında değiştirilebilir.
        """
        with self.lock:
            if self._dip_locked():
                return False, ("DIP switch'ler tur sırasında kilitli. "
                               "Turlar arasında değiştirebilirsiniz.")
            if robot is None:
                self.dips = [int(dip) & 0x7, int(dip) & 0x7]
            elif 0 <= robot < len(self.dips):
                self.dips[robot] = int(dip) & 0x7
            else:
                return False, "Robot bulunamadı."
            return True, None


# Tek global motor (FastAPI tarafindan kullanilir)
ENGINE = SimulationEngine()
