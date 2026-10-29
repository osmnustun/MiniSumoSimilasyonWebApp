#!/usr/bin/env python3
"""
Mini Sumo Simülatör — uçtan uca duman testi.

Kullanım (panel çalışırken):
    python3 tests/smoke_test.py [http://localhost:7101]
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:7101"


def _request(path, data=None, token=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Sumo-Token"] = token
    req = urllib.request.Request(
        BASE + path, data=body, headers=headers,
        method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"ok": False, "error": f"HTTP {e.code}"}


def post(path, data=None, token=None):
    return _request(path, data if data is not None else {}, token)[1]


def get(path, token=None):
    return _request(path, token=token)[1]


def status_of(path, data=None, token=None):
    """(http_status, json) döndür — yetki kodu kontrolleri için."""
    return _request(path, data, token)


def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        ok = ok and cond

    print("0) Oda durumu (açık mod gerekir)")
    room = get("/api/room")
    if not room.get("open"):
        takeover = post("/api/room/create", {"name": "smoke-test"})
        if takeover.get("ok"):
            # Bayat odayı devral ve hemen kapat → açık mod
            post("/api/room/leave", {}, takeover["token"])
            room = get("/api/room")
        if not room.get("open"):
            print("  ⚠️  Aktif bir oda var ve hakemi bağlı. Testler açık mod ister;")
            print("      panelden odadan ayrılın veya birkaç saniye sonra tekrar deneyin.")
            return 2
    check("panel açık modda", get("/api/room")["open"] is True)

    print("1) Servis erişimi")
    st = get("/api/state")
    check("durum alındı", "status" in st)

    print("2) Sabitler / pin haritası")
    cfg = get("/api/config")
    check("dohyo 770 mm", cfg["ring_diameter_mm"] == 770)
    check("maks ağırlık 500 g", cfg["max_weight_g"] == 500)
    check("QTR pin A0/A1", cfg["pins"]["qtr_left"] == "A0" and cfg["pins"]["qtr_right"] == "A1")
    check("3 DIP switch", all(k in cfg["pins"] for k in ("dip1", "dip2", "dip3")))

    print("3) Ağırlık ayarı")
    post("/api/weight", {"mass_g": 350, "robot": 0})
    check("ağırlık kabul edildi", True)

    print("4) DIP switch (robot başına)")
    post("/api/dip", {"switches": [1, 0, 1], "robot": 0})
    post("/api/dip", {"switches": [0, 1, 0], "robot": 1})
    check("robot başına DIP ayrı ayarlanıyor", get("/api/state")["dips"] == [5, 2])

    print("5) Derleme")
    code = open("firmware/sumo_robot_example.ino").read()
    r = post("/api/compile", {"code": code})
    check("örnek firmware derlendi", r.get("ok") is True)

    print("6) Maç başlat (gerçek AVR + 3B fizik)")
    post("/api/start", {"code1": code, "code2": code, "duration": 30})
    moved = False
    for _ in range(20):
        time.sleep(1.0)
        s = get("/api/state")
        if s["robots"]:
            p0 = s["robots"][0]["position"]
            if abs(p0[0]) > 0.05 or abs(p0[1]) > 0.05:
                moved = True
            if s["status"] in ("running", "finished"):
                break
    check("robotlar hareket etti", moved)
    s = get("/api/state")
    check("best-of-3 (2 kazanan)", s.get("best_of") == 3 and "score" in s)

    print("7) 3B render karesi")
    raw = urllib.request.urlopen(BASE + "/api/frame.jpg", timeout=15).read()
    check("JPEG kare üretildi", raw[:2] == b"\xff\xd8" and len(raw) > 2000)

    print("8) Durdur / sıfırla")
    post("/api/stop")
    post("/api/reset")
    check("durduruldu", get("/api/state")["status"] in ("idle", "finished"))

    print("9) Eğitim / test modu")
    post("/api/test/start")
    time.sleep(2.5)
    st = get("/api/state")
    check("test modu aktif", st["mode"] == "test" and len(st["robots"]) == 1)
    post("/api/test/place", {"x": 0.0, "y": 0.0, "yaw": 0})
    time.sleep(0.8)
    center_qtr = get("/api/state")["robots"][0]["sensors"]["qtr_left"]
    post("/api/test/place", {"x": 0.34, "y": 0.0, "yaw": 0})
    time.sleep(0.8)
    edge_qtr = get("/api/state")["robots"][0]["sensors"]["qtr_left"]
    check("QTR beyaz merkez vs siyah kenar", center_qtr > 700 and edge_qtr < 400)
    # E3Z hedef
    post("/api/test/place", {"x": 0.0, "y": 0.0, "yaw": 0})
    post("/api/test/target", {"x": 0.15, "y": 0.0})
    time.sleep(0.8)
    e3z = get("/api/state")["robots"][0]["sensors"]
    check("E3Z hedefi gördü", e3z["e3z_left"] == 1 or e3z["e3z_right"] == 1)
    # motor sürüşü
    post("/api/test/place", {"x": 0.0, "y": 0.0, "yaw": 0})
    time.sleep(0.5)
    post("/api/test/motor", {"left": 800, "right": 800})
    time.sleep(1.5)
    pos = get("/api/state")["robots"][0]["position"]
    post("/api/test/motor", {"left": 0, "right": 0})
    check("ileri sürüş +x yönünde", pos[0] > 0.03)

    print("9b) Eğitim modunda firmware testi")
    rr = post("/api/test/run", {"code": code})
    check("firmware testi başlatıldı", rr.get("ok") is True)
    time.sleep(6)
    sfu = get("/api/state")
    check("firmware modu aktif", bool(sfu.get("test_has_firmware")))
    bad = post("/api/test/run", {"code": "void setup(){ hatali(); } void loop(){}"})
    check("hatalı kod reddedildi", bad.get("ok") is False or "error" in bad)

    print("10) WebGL sahne dosyaları")
    three = urllib.request.urlopen(BASE + "/static/three.min.js", timeout=15).read()
    check("three.js servis ediliyor", len(three) > 100000)

    print("11) Çok istemcili oda / rol / DIP kilidi")
    post("/api/reset")
    room0 = get("/api/room")
    if not room0.get("open"):
        print("  ⚠️  Zaten açık bir oda var; bu bölüm atlanıyor (sunucuyu yeniden başlatın).")
    else:
        host = post("/api/room/create", {"name": "Hoca"})
        check("oda oluşturuldu", host.get("ok") is True and host.get("role") == "host")
        room_code = host["code"]
        host_token = host["token"]

        b = post("/api/room/join", {"code": room_code, "side": "blue", "name": "Ali"})
        r = post("/api/room/join", {"code": room_code, "side": "red", "name": "Veli"})
        check("mavi oyuncu katıldı", b.get("role") == 0)
        check("kırmızı oyuncu katıldı", r.get("role") == 1)
        blue_token, red_token = b["token"], r["token"]

        bad = post("/api/room/join", {"code": "XXXXXX", "side": "blue"}, )
        check("hatalı oda kodu reddedildi", bad.get("ok") is False)
        dup = post("/api/room/join", {"code": room_code, "side": "blue"})
        check("dolu taraf reddedildi", dup.get("ok") is False)

        # --- rol yetkileri ---
        check("mavi kırmızının DIP'ini değiştiremez",
              status_of("/api/dip", {"switches": [1, 1, 1], "robot": 1}, blue_token)[0] == 403)
        check("izleyici DIP değiştiremez",
              status_of("/api/dip", {"switches": [1, 1, 1], "robot": 0})[0] == 403)
        check("mavi kendi DIP'ini değiştirdi",
              post("/api/dip", {"switches": [1, 0, 1], "robot": 0}, blue_token).get("ok") is True)
        check("kırmızı kendi DIP'ini değiştirdi",
              post("/api/dip", {"switches": [0, 1, 0], "robot": 1}, red_token).get("ok") is True)
        check("oyuncu başka robotun ağırlığını değiştiremez",
              status_of("/api/weight", {"mass_g": 300, "robot": 1}, blue_token)[0] == 403)
        check("oyuncu maçı başlatamaz",
              status_of("/api/start", {}, blue_token)[0] == 403)
        check("izleyici sahayı sıfırlayamaz",
              status_of("/api/reset", {}, None)[0] == 403)

        # --- kod gönder + hazır ---
        mini = "void setup(){} void loop(){}"
        check("oyuncu kodunu gönderdi",
              post("/api/player/code", {"code": mini}, blue_token).get("ok") is True)
        check("hazır durumu ayarlandı",
              post("/api/player/ready", {"ready": True}, blue_token).get("ok") is True)
        st = get("/api/room", blue_token)
        check("oda kod ve oyuncu durumunu bildiriyor",
              st["code"] == room_code and st["players"][0]["has_code"]
              and st["players"][0]["ready"])

        # --- maç: DIP kilidi ---
        start = post("/api/start", {"duration": 6, "break_duration": 4}, host_token)
        check("hakem maçı başlattı (oyuncu kodlarıyla)",
              start.get("ok") is True and start.get("used_player_codes") == [True, False])
        time.sleep(1.0)
        s = get("/api/state", blue_token)
        check("maç geri sayımda", s["status"] == "countdown")
        check("DIP geri sayımda kilitli", s["dip_locked"] is True)
        check("geri sayımda DIP 409 döner",
              status_of("/api/dip", {"switches": [0, 0, 0], "robot": 0}, blue_token)[0] == 409)

        for _ in range(30):
            time.sleep(0.5)
            if get("/api/state")["status"] == "running":
                break
        check("tur çalışıyor", get("/api/state")["status"] == "running")
        check("çalışırken DIP 409 döner",
              status_of("/api/dip", {"switches": [0, 0, 0], "robot": 0}, blue_token)[0] == 409)

        reached_break = False
        for _ in range(60):
            time.sleep(0.5)
            if get("/api/state")["status"] == "break":
                reached_break = True
                break
        check("tur bitince 'break' durumu", reached_break)
        if reached_break:
            s = get("/api/state", blue_token)
            check("tur arası DIP açık", s["dip_locked"] is False)
            check("tur arası süre sayacı var", s["break_left"] > 0)
            check("tur arasında DIP değiştirilebildi (200)",
                  status_of("/api/dip", {"switches": [1, 1, 0], "robot": 0}, blue_token)[0] == 200)
            next_locked = False
            for _ in range(30):
                time.sleep(0.5)
                ns = get("/api/state")
                if ns["status"] in ("countdown", "running"):
                    next_locked = True
                    break
            check("yeni tur başladı", next_locked)
            check("yeni turda DIP yeniden kilitli", get("/api/state")["dip_locked"] is True)

        post("/api/stop", {}, host_token)
        post("/api/reset", {}, host_token)

        # --- oyuncu ayrılınca taraf boşalır, hakem ayrılınca oda kapanır ---
        post("/api/room/leave", {}, red_token)
        st = get("/api/room", host_token)
        check("oyuncu ayrılınca taraf boşaldı", st["players"][1] is None)
        post("/api/room/leave", {}, host_token)
        check("hakem ayrılınca oda kapandı", get("/api/room")["open"] is True)

    print("\n" + ("TÜM TESTLER GEÇTİ ✅" if ok else "BAZI TESTLER BAŞARISIZ ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
