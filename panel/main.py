# panel/main.py
"""
Mini Sumo Simulator — web paneli ve REST API.

Çok istemcili (multi-client) maç altyapısı:
  * Hakem oda oluşturur, oda kodu + yönetici token'ı alır.
  * İki öğrenci farklı bilgisayarlardan oda koduna ve tarafına (mavi/kırmızı)
    katılır; her biri kendi kodunu ve DIP switch'lerini yönetir.
  * DIP switch'ler tur sırasında kilitlidir; yalnızca turlar arasında ve
    maç aralarında değiştirilebilir.
"""
import os
import sys
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Header
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.sim_engine import ENGINE
from bridge.firmware import compile_sketch, FirmwareError
from panel.room import ROOM, ROLE_LABELS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_DIR = os.path.join(BASE_DIR, "firmware")
PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIRMWARE = os.path.join(FIRMWARE_DIR, "sumo_robot_example.ino")

app = FastAPI(title="Mini Sumo Simulator")

STATIC_DIR = os.path.join(PANEL_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- yardımcılar
def _default_code() -> str:
    try:
        with open(DEFAULT_FIRMWARE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "void setup(){} void loop(){}"


def _who(token):
    """(kind, role, player) — kind: host | player | spectator"""
    return ROOM.resolve(token)


def _forbidden(msg="Bu işlem için yetkiniz yok."):
    return JSONResponse(status_code=403, content={"ok": False, "error": msg})


def _locked(msg, **extra):
    payload = {"ok": False, "error": msg, "dip_locked": ENGINE.dip_locked,
               "dips": list(ENGINE.dips)}
    payload.update(extra)
    return JSONResponse(status_code=409, content=payload)


def _can_use_test_lab(kind) -> bool:
    """Test/eğitim laboratuvarı maç sürerken kullanılamaz."""
    if kind == "spectator":
        return False
    if ENGINE.mode == "match" and ENGINE.status in ("countdown", "running", "break"):
        return False
    return True


# ---------------------------------------------------------------- modeller
class WeightConfig(BaseModel):
    mass_g: float
    robot: Optional[int] = None  # None -> hepsi (yalnızca hakem)


class DipConfig(BaseModel):
    switches: Optional[list] = None  # [0/1, 0/1, 0/1]
    value: Optional[int] = None      # bitmask
    robot: Optional[int] = None      # 0/1 -> ilgili robot; None -> hepsi (hakem)


class CodeConfig(BaseModel):
    code: str
    name: Optional[str] = None


class StartConfig(BaseModel):
    code1: Optional[str] = None
    code2: Optional[str] = None
    duration: float = 180.0
    break_duration: float = 12.0


class SaveConfig(BaseModel):
    name: str
    content: str


class ManualMotor(BaseModel):
    left: int = 0
    right: int = 0


class PlaceConfig(BaseModel):
    x: float
    y: float
    yaw: float = 0.0


class RoomCreate(BaseModel):
    name: Optional[str] = "Hakem"


class RoomJoin(BaseModel):
    code: str
    side: str                       # "blue" | "red"
    name: Optional[str] = None


class ReadyConfig(BaseModel):
    ready: bool = True


class PlayerCode(BaseModel):
    code: str
    robot: Optional[int] = None     # yalnızca hakem başka robotun kodunu yazabilir


# ---------------------------------------------------------------- sayfalar
@app.get("/")
async def root():
    with open(os.path.join(PANEL_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---------------------------------------------------------------- oda / oyuncular
@app.get("/api/room")
async def room_state(x_sumo_token: Optional[str] = Header(None)):
    return ROOM.state(x_sumo_token)


@app.post("/api/room/create")
async def room_create(cfg: RoomCreate,
                      x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not ROOM.open and kind != "host":
        if ROOM.host_connected():
            return _forbidden("Aktif bir oda var. Yeni oda oluşturmak için hakem olun.")
        # Hakem kopuk → bayat oda: devral ve yeni oda aç
        ROOM.close()
    info = ROOM.create(cfg.name)
    ENGINE.reset()                  # yeni oda = temiz saha
    return {"ok": True, **info, "room": ROOM.state(info["token"])}


@app.post("/api/room/join")
async def room_join(cfg: RoomJoin):
    info, err = ROOM.join(cfg.code, cfg.side, cfg.name)
    if err:
        return JSONResponse(status_code=409, content={"ok": False, "error": err})
    return {"ok": True, **info, "room": ROOM.state(info["token"])}


@app.post("/api/room/leave")
async def room_leave(x_sumo_token: Optional[str] = Header(None)):
    ROOM.leave(x_sumo_token)
    return {"ok": True}


@app.post("/api/player/ready")
async def player_ready(cfg: ReadyConfig,
                       x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind != "player":
        return _forbidden("Yalnızca oyuncular hazır durumunu değiştirebilir.")
    info = ROOM.set_ready(role, cfg.ready)
    return {"ok": True, "player": info}


@app.post("/api/player/code")
async def player_code(cfg: PlayerCode,
                      x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler kod gönderemez.")
    if kind == "host":
        target = cfg.robot
        if target not in (0, 1):
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": "robot 0 veya 1 olmalı"})
    else:
        target = role
    if not ROOM.set_code(target, cfg.code):
        return JSONResponse(status_code=409,
                            content={"ok": False, "error": "Oyuncu odada değil."})
    return {"ok": True, "robot": target, "has_code": True}


@app.get("/api/player/code")
async def get_player_code(robot: Optional[int] = None,
                          x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler kodu göremez.")
    if kind == "host":
        target = robot if robot in (0, 1) else 0
    else:
        target = role
    return {"ok": True, "robot": target, "code": ROOM.get_code(target),
            "name": ROLE_LABELS.get(target, "")}


# ---------------------------------------------------------------- durum
@app.get("/api/state")
async def state(x_sumo_token: Optional[str] = Header(None)):
    # Heartbeat: oyuncu/hakem bağlılık takibi
    kind, role, player = _who(x_sumo_token)
    st = ENGINE.get_state()
    st["you"] = {
        "kind": kind,
        "role": role,
        "name": player.name if player else (
            ROOM.host_name if kind == "host" else "İzleyici"),
    }
    return st


@app.get("/api/frame.jpg")
async def frame():
    data = ENGINE.get_frame()
    if data is None:
        # bos kare
        from PIL import Image
        import io
        img = Image.new("RGB", (800, 533), (10, 14, 23))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        data = buf.getvalue()
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- kontrol
@app.post("/api/weight")
async def set_weight(cfg: WeightConfig,
                     x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler ağırlık değiştiremez.")
    if kind == "player":
        # Eğitim laboratuvarında tek (0 numaralı) test robotu vardır; iki oyuncu da kullanır.
        if ENGINE.mode == "test" and cfg.robot in (None, 0):
            target = 0
        elif cfg.robot is not None and cfg.robot != role:
            return _forbidden("Yalnızca kendi robotunuzun ağırlığını değiştirebilirsiniz.")
        else:
            target = role
    else:
        target = cfg.robot

    if target is None:
        ok, err = ENGINE.set_all_weights(cfg.mass_g)
    else:
        ok, err = ENGINE.set_weight(target, cfg.mass_g)
    if not ok:
        return JSONResponse(status_code=409,
                            content={"ok": False, "error": err,
                                     "weight_locked": ENGINE.weight_locked})
    return {"ok": True, "mass_g": cfg.mass_g, "robot": target,
            "weight_locked": ENGINE.weight_locked}


@app.post("/api/dip")
async def set_dip(cfg: DipConfig,
                  x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler DIP switch değiştiremez.")
    if kind == "player":
        # Eğitim laboratuvarında tek (0 numaralı) test robotu vardır; iki oyuncu da kullanır.
        if ENGINE.mode == "test" and cfg.robot in (None, 0):
            target = 0
        elif cfg.robot is not None and cfg.robot != role:
            return _forbidden("Yalnızca kendi DIP switch'lerinizi değiştirebilirsiniz.")
        else:
            target = role
    else:
        target = cfg.robot

    if cfg.switches is not None:
        val = 0
        for i, s in enumerate(cfg.switches[:3]):
            if s:
                val |= (1 << i)
    elif cfg.value is not None:
        val = cfg.value
    else:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": "switches veya value gerekli"})

    ok, err = ENGINE.set_dip(val, robot=target)
    if not ok:
        return _locked(err)
    return {"ok": True, "dips": list(ENGINE.dips), "dip_locked": ENGINE.dip_locked}


@app.post("/api/start")
async def start(cfg: StartConfig,
                x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind != "host":
        return _forbidden("Maçı yalnızca hakem başlatabilir.")

    # Öncelik: açıkça verilen kod > oyuncunun gönderdiği kod > örnek firmware
    pc1, pc2 = ROOM.get_code(0), ROOM.get_code(1)
    c1 = cfg.code1 or pc1 or _default_code()
    c2 = cfg.code2 or pc2 or _default_code()
    ENGINE.start(c1, c2, duration=cfg.duration,
                 break_duration=cfg.break_duration)
    ROOM.reset_ready()
    return {"ok": True,
            "used_player_codes": [bool(cfg.code1 is None and pc1),
                                  bool(cfg.code2 is None and pc2)]}


@app.post("/api/stop")
async def stop(x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind != "host":
        return _forbidden("Maçı yalnızca hakem durdurabilir.")
    ENGINE.stop()
    return {"ok": True}


# ---------------------------------------------------------------- test / eğitim
@app.post("/api/test/start")
async def test_start(x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not _can_use_test_lab(kind):
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": "Maç sürerken eğitim laboratuvarı kullanılamaz."})
    ENGINE.start_test()
    return {"ok": True}


@app.post("/api/test/run")
async def test_run(cfg: CodeConfig,
                   x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not _can_use_test_lab(kind):
        return JSONResponse(status_code=409, content={
            "ok": False,
            "error": "Maç sürerken eğitim laboratuvarı kullanılamaz."})
    try:
        ENGINE.start_test_firmware(cfg.code)
        return {"ok": True, "message": "Firmware testi başlatıldı"}
    except FirmwareError as e:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})


@app.post("/api/test/motor")
async def test_motor(cfg: ManualMotor,
                     x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not _can_use_test_lab(kind):
        return _forbidden("Maç sürerken manuel kontrol kullanılamaz.")
    ENGINE.set_manual_motors(cfg.left, cfg.right)
    return {"ok": True, "left": cfg.left, "right": cfg.right}


@app.post("/api/test/place")
async def test_place(cfg: PlaceConfig,
                     x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not _can_use_test_lab(kind):
        return _forbidden("Maç sürerken robot yerleştirilemez.")
    ENGINE.place_robot(cfg.x, cfg.y, cfg.yaw)
    return {"ok": True}


@app.post("/api/test/target")
async def test_target(cfg: PlaceConfig,
                      x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if not _can_use_test_lab(kind):
        return _forbidden("Maç sürerken hedef yerleştirilemez.")
    ENGINE.set_target(cfg.x, cfg.y)
    return {"ok": True}


@app.post("/api/reset")
async def reset(x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind != "host":
        return _forbidden("Sahayı yalnızca hakem sıfırlayabilir.")
    ENGINE.reset()
    return {"ok": True}


@app.post("/api/compile")
async def compile_check(cfg: CodeConfig):
    try:
        compile_sketch(cfg.code, "sumo_check")
        return {"ok": True, "message": "Derleme başarılı"}
    except FirmwareError as e:
        return JSONResponse(status_code=400,
                            content={"ok": False, "error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": str(e)})


# ---------------------------------------------------------------- firmware dosyalari
@app.get("/api/firmware")
async def list_firmware():
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(FIRMWARE_DIR) if f.endswith(".ino"))
    return {"files": files}


@app.get("/api/firmware/{name}")
async def get_firmware(name: str):
    path = os.path.join(FIRMWARE_DIR, os.path.basename(name))
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "bulunamadı"})
    with open(path, "r", encoding="utf-8") as f:
        return {"name": name, "content": f.read()}


@app.post("/api/firmware")
async def save_firmware(cfg: SaveConfig,
                        x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler dosya kaydedemez.")
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    name = os.path.basename(cfg.name)
    if not name.endswith(".ino"):
        name += ".ino"
    with open(os.path.join(FIRMWARE_DIR, name), "w", encoding="utf-8") as f:
        f.write(cfg.content)
    return {"ok": True, "name": name}


@app.post("/api/upload")
async def upload_firmware(file: UploadFile = File(...),
                          x_sumo_token: Optional[str] = Header(None)):
    kind, role, player = _who(x_sumo_token)
    if kind == "spectator":
        return _forbidden("İzleyiciler dosya yükleyemez.")
    os.makedirs(FIRMWARE_DIR, exist_ok=True)
    name = os.path.basename(file.filename)
    content = (await file.read()).decode("utf-8", errors="replace")
    with open(os.path.join(FIRMWARE_DIR, name), "w", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "name": name, "content": content}


# ---------------------------------------------------------------- sabitler
@app.get("/api/config")
async def config():
    return {
        "ring_diameter_mm": 770,
        "edge_width_mm": 25,
        "max_weight_g": 500,
        "min_weight_g": 100,
        "motor_rpm": 250,
        "motor_voltage": 6.0,
        "battery": "3S LiPo 11.1V",
        "best_of": 3,
        "wins_needed": 2,
        "countdown_s": 5.0,
        "break_duration_s": ENGINE.break_duration,
        "dip_lock_rule": "DIP switch'ler yalnızca turlar arasında ve maç aralarında değiştirilebilir.",
        "pins": {
            "qtr_left": "A0", "qtr_right": "A1",
            "e3z_left": "D2", "e3z_right": "D3",
            "motor_left_pwm": "D9", "motor_right_pwm": "D10",
            "motor_left_dir": ["D7", "D8"], "motor_right_dir": ["D4", "D12"],
            "motor_enable": "D6",
            "dip1": "A2", "dip2": "A3", "dip3": "A4",
            "vbat": "A5", "button": "D11",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7101, workers=1)
