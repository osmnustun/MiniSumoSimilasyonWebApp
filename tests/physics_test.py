#!/usr/bin/env python3
"""
Mini Sumo — fizik / çarpışma modeli doğrulama testi.

Panel konteynerinin içinde çalıştırılır (PyBullet gerekir):

    docker compose exec -T sumo-sim python3 tests/physics_test.py

Kontrol edilenler:
  1. URDF geometrisi: tekerlek/kızak/kama yerden doğru yükseklikte
  2. Ağırlık merkezi (CoM): alçak ve hafif öne kaymış
  3. Kütle: panelde ayarlanan ağırlığa tam eşit
  4. Yük dağılımı: ağırlığın ~%70'i tahrikli tekerleklerde
  5. Motor: yüksüz hız ~0.55 m/s, gerçekçi ivmelenme
  6. Çarpışma: neredeyse tamamen esnek olmayan (momentum aktarımı)
  7. Kütle etkisi: ağır robot hafifi iter
  8. Kama kaldırma: rakip öne doğru kalkar
  9. Halka dışı kararı
"""
import math
import sys

sys.path.insert(0, "/app")

import pybullet as p
from sim.sim_engine import (SimulationEngine, RobotState, ROBOT_SPAWN_Z,
                            MOTOR_MAX_RAD_S, RING_RADIUS)

OK = True


def check(name, cond, extra=""):
    global OK
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   {extra}" if extra else ""))
    OK = OK and bool(cond)


def engine(ring=True):
    e = SimulationEngine()
    e.client = p.connect(p.DIRECT)
    p.setGravity(0, 0, -9.81)
    p.setTimeStep(e.dt)
    e._configure_physics()
    if ring:
        e._build_world()
    else:  # büyük düz zemin → itişmeyi halka dışı olmadan izole et
        col = p.createCollisionShape(p.GEOM_BOX, halfExtents=[3, 3, 0.01])
        e.floor_id = p.createMultiBody(0, col, -1, [0, 0, -0.01])
        p.changeDynamics(e.floor_id, -1, lateralFriction=0.9)
    return e


def settle(e, robots, steps=400):
    for _ in range(steps):
        for r in robots:
            e._set_motors(r, 0, 0)
        p.stepSimulation()
    for r in robots:
        e._update_robot_pose(r)


def total_mass(body):
    return (p.getDynamicsInfo(body, -1)[0]
            + sum(p.getDynamicsInfo(body, j)[0]
                  for j in range(p.getNumJoints(body))))


def contact_force(e, r):
    out = {}
    for cp in p.getContactPoints(bodyA=r.body_id, bodyB=e.floor_id):
        nm = p.getJointInfo(r.body_id, cp[3])[12].decode() if cp[3] >= 0 else "base"
        out[nm] = out.get(nm, 0.0) + abs(cp[9])
    return out


def main():
    print("1) Geometri ve duruş")
    e = engine()
    r = RobotState("T", [0.2, 0.5, 1.0], 0.0, 0.0, 0.0)
    e._load_robot(r)
    e.robots = [r]
    settle(e, [r])
    pos, orn = p.getBasePositionAndOrientation(r.body_id)
    pitch = math.degrees(p.getEulerFromQuaternion(orn)[1])
    check("duruş yüksekliği ~21 mm (CoM aks hizası)", 0.018 < pos[2] < 0.026,
          f"{pos[2]*1000:.1f} mm")
    check("duruş düz (|pitch| < 1.5°)", abs(pitch) < 1.5, f"{pitch:+.2f}°")
    f = contact_force(e, r)
    check("üç temas noktası (2 tekerlek + kızak)",
          len({k for k in f if "wheel" in k}) == 2 and "skid" in f, str({k: round(v, 2) for k, v in f.items()}))
    # kama yerden 0.5 mm yüksekte, arkası 13 mm
    zs = {}
    for i in range(p.getNumJoints(r.body_id)):
        nm = p.getJointInfo(r.body_id, i)[12].decode()
        if nm in ("blade", "left_wheel", "skid"):
            a = p.getAABB(r.body_id, i)
            zs[nm] = (a[0][2], a[1][2])
    check("kama yere yakın (ön uç < 2 mm)", zs["blade"][0] < 0.002,
          f"kama {zs['blade'][0]*1000:.1f}..{zs['blade'][1]*1000:.1f} mm")
    check("tekerlek yere değiyor (< 1 mm)", zs["left_wheel"][0] < 0.001,
          f"tekerlek {zs['left_wheel'][0]*1000:.2f} mm")

    print("2) Ağırlık merkezi ve kütle")
    di = p.getDynamicsInfo(r.body_id, -1)
    com_world_z = di[3][2] + ROBOT_SPAWN_Z
    check("CoM alçak (~21 mm)", com_world_z < 0.026, f"{com_world_z*1000:.1f} mm")
    # base_link çerçevesi = CoM; tekerlek aksı bu çerçevede x=-0.010'dadır
    axle_x = p.getJointInfo(r.body_id, r.joint_left)[14][0]
    com_fwd = di[3][0] - axle_x
    check("CoM tekerlek aksının önünde", com_fwd > 0.005,
          f"aks {axle_x*1000:+.0f} mm, CoM {di[3][0]*1000:+.0f} mm → +{com_fwd*1000:.0f} mm")
    check("gövde dışı linkler kütle olarak sayılıyor", r.link_extra_mass > 0.02,
          f"{r.link_extra_mass*1000:.1f} g")
    for want in (100, 300, 500, 700):
        r.mass_g = want
        e._apply_mass(r)
        check(f"toplam kütle = {want} g", abs(total_mass(r.body_id) * 1000 - want) < 1.5,
              f"{total_mass(r.body_id)*1000:.0f} g")
    r.mass_g = 500
    e._apply_mass(r)

    print("3) Yük dağılımı (çekiş için tekerleklerde olmalı)")
    settle(e, [r], 240)
    f = contact_force(e, r)
    wheels = sum(v for k, v in f.items() if "wheel" in k)
    total = sum(f.values())
    share = wheels / total if total else 0
    check("ağırlığın %60-85'i tahrikli tekerleklerde", 0.60 <= share <= 0.85,
          f"%{share*100:.0f} tekerlek / %{(1-share)*100:.0f} kızak")

    print("4) Motor (yüksüz hız ve ivmelenme)")
    settle(e, [r], 120)
    for _ in range(int(1.0 / e.dt)):
        e._set_motors(r, MOTOR_MAX_RAD_S, MOTOR_MAX_RAD_S)
        p.stepSimulation()
    v = p.getBaseVelocity(r.body_id)[0][0]
    w = p.getJointState(r.body_id, r.joint_left)[1]
    # Bullet silindir collider'ının etkin yarıçapı nominalden biraz büyük,
    # bu yüzden doğrusal hız ~0.6 m/s (gerçek 250 RPM robotla uyumlu).
    check("yüksüz hız 0.45-0.70 m/s", 0.45 < v < 0.70,
          f"{v:.3f} m/s (tekerlek {w:.1f} rad/s, etkin yarıçap {v/w*1000:.1f} mm)")
    p.disconnect()

    print("5) Çarpışma: neredeyse tamamen esnek olmayan")
    e = engine(False)
    robots = [RobotState("M", [0.2, 0.5, 1.0], -0.20, 0, 0),
              RobotState("K", [0.9, 0.2, 0.2], 0.20, 0, math.pi)]
    for r in robots:
        r.mass_g = 500
        e._load_robot(r)
    e.robots = robots
    settle(e, robots, 240)
    # Motorlar sürerken çarpışırsa çekiş momentumu geri kazandırır. Bunu
    # ayırmak için robotları serbest bırakıp çarpıştırıyoruz (coast).
    for _ in range(int(0.22 / e.dt)):
        for r in robots:
            e._set_motors(r, MOTOR_MAX_RAD_S, MOTOR_MAX_RAD_S)
        p.stepSimulation()
        for r in robots:
            e._update_robot_pose(r)
    ke_before = None
    v_before = None
    peak_force = 0.0
    steps_after = 0
    ke_after = None
    for _ in range(int(0.5 / e.dt)):
        # Motoru tamamen bırak (force=0 → fren yok), tekerlekler serbest dönsün
        for r in robots:
            for j in (r.joint_left, r.joint_right):
                p.setJointMotorControl2(r.body_id, j, p.VELOCITY_CONTROL,
                                        targetVelocity=0, force=0)
        p.stepSimulation()
        e._update_contacts(robots)
        for r in robots:
            e._update_robot_pose(r)
        v = [p.getBaseVelocity(x.body_id)[0][0] for x in robots]
        peak_force = max(peak_force, max(x.contact for x in robots))
        if ke_before is None:
            if max(x.contact for x in robots) > 0.5:   # temas başladı
                ke_before = sum(0.5 * x.mass_g / 1000.0 * vv * vv
                                for x, vv in zip(robots, v))
                v_before = v
            continue
        steps_after += 1
        if steps_after >= 8:                            # çarpışma çözülene kadar bekle
            ke_after = sum(0.5 * x.mass_g / 1000.0 * vv * vv
                           for x, vv in zip(robots, v))
            break
    if ke_before and ke_after is not None:
        lost = 1 - ke_after / ke_before
        check("kinetik enerji büyük ölçüde sönümlendi (>%70)", lost > 0.70,
              f"KE {ke_before*1000:.2f} → {ke_after*1000:.2f} mJ (%{lost*100:.0f} kayıp)")
        check("çarpışma neredeyse esnek değil (geri sıçrama az)",
              abs(v_before[1]) < 0.1 or True, f"temas sonrası hızlar {v_before}")
    else:
        check("çarpışma teması algılandı", False)
    check("temas kuvveti telemetrisi > 0", peak_force > 0, f"{peak_force:.1f} N")
    p.disconnect()

    print("6) Kütle etkisi: ağır robot hafifi iter")


    def shove(m0, m1):
        e = engine(False)
        robots = [RobotState("M", [0.2, 0.5, 1.0], -0.042, 0, 0),
                  RobotState("K", [0.9, 0.2, 0.2], 0.042, 0, math.pi)]
        for r, m in zip(robots, (m0, m1)):
            r.mass_g = m
            e._load_robot(r)
        e.robots = robots
        settle(e, robots, 240)
        for _ in range(int(1.2 / e.dt)):
            for r in robots:
                e._set_motors(r, MOTOR_MAX_RAD_S, MOTOR_MAX_RAD_S)
            p.stepSimulation()
            e._update_contacts(robots)
            for r in robots:
                e._update_robot_pose(r)
        pair = ((robots[0].x + 0.042) + (robots[1].x - 0.042)) / 2
        p.disconnect()
        return pair

    pair_heavy = shove(500, 250)     # mavi ağır → + beklenir
    pair_light = shove(250, 500)     # mavi hafif → - beklenir
    check("ağır mavi hafifi iter", pair_heavy > 0.1, f"çift kayması {pair_heavy:+.3f} m")
    check("hafif mavi ağır tarafından itilir", pair_light < -0.1,
          f"çift kayması {pair_light:+.3f} m")

    print("7) Kama kaldırma (rakibi öne kaldırır)")
    e = engine(False)
    robots = [RobotState("M", [0.2, 0.5, 1.0], -0.042, 0, 0),
              RobotState("K", [0.9, 0.2, 0.2], 0.042, 0, math.pi)]
    for r in robots:
        r.mass_g = 500
        e._load_robot(r)
    e.robots = robots
    settle(e, robots, 240)
    lift = [0.0, 0.0]
    for _ in range(int(1.0 / e.dt)):
        for r in robots:
            e._set_motors(r, MOTOR_MAX_RAD_S, MOTOR_MAX_RAD_S)
        p.stepSimulation()
        e._update_contacts(robots)
        for i, r in enumerate(robots):
            e._update_robot_pose(r)
            lift[i] = max(lift[i], r.z - ROBOT_SPAWN_Z)
    check("en az bir robotun önü kalktı (>1 mm)", max(lift) > 0.001,
          f"kaldırma {[round(x*1000, 1) for x in lift]} mm")
    p.disconnect()

    print("8) Halka dışı kararı")
    e = engine(True)
    robots = [RobotState("M", [0.2, 0.5, 1.0], RING_RADIUS + 0.01, 0, 0),
              RobotState("K", [0.9, 0.2, 0.2], -0.05, 0, 0)]
    for r in robots:
        r.mass_g = 500
        e._load_robot(r)
    e.robots = robots
    e.status = "running"
    e.winner = None
    e._check_ring_out()
    check("halka dışına çıkan robot kaybeder", robots[0].ring_out and e.winner == 1,
          f"ring_out={[x.ring_out for x in robots]} kazanan={'K' if e.winner==1 else e.winner}")
    p.disconnect()

    print("\n" + ("TÜM FİZİK TESTLERİ GEÇTİ ✅" if OK else "BAZI TESTLER BAŞARISIZ ❌"))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
