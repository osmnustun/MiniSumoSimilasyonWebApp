# panel/room.py
"""
Maç odası / oyuncu yönetimi.

İki öğrenci kendi bilgisayarlarından aynı odaya bağlanır:
  * Hakem (host) oda oluşturur, kodu paylaşır, maçı başlatır.
  * Mavi ve kırmızı oyuncular oda koduyla kendi taraflarına katılır.
  * Herkes aynı canlı 3B maçı izler; her oyuncu yalnızca kendi
    DIP switch'lerini ve kendi kodunu değiştirebilir.

Token tabanlıdır: tarayıcı localStorage'da token tutar, isteklerde
`X-Sumo-Token` başlığıyla gönderir. Oda oluşturulmadan önce sistem
"açık mod"dadır (eski tek kullanıcılı davranış / testler için).
"""
import secrets
import threading
import time

# Karışabilecek karakterler (0/O, 1/I) yok
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROLE_SIDES = {0: "blue", 1: "red"}
ROLE_LABELS = {0: "Mavi", 1: "Kırmızı"}
CONNECT_TIMEOUT = 15.0       # sn; bu süre heartbeat gelmezse bağlantı kopuk sayılır


def _clean_name(name, fallback):
    name = (name or "").strip()
    return name[:40] if name else fallback


class Player:
    def __init__(self, role, name, token):
        self.role = role
        self.name = _clean_name(name, ROLE_LABELS[role])
        self.token = token
        self.ready = False
        self.code = None
        self.code_updated = 0.0
        self.last_seen = time.time()

    def connected(self):
        return (time.time() - self.last_seen) < CONNECT_TIMEOUT

    def to_dict(self):
        return {
            "role": self.role,
            "side": ROLE_SIDES[self.role],
            "name": self.name,
            "connected": self.connected(),
            "ready": bool(self.ready),
            "has_code": bool(self.code),
        }


class RoomManager:
    def __init__(self):
        # state() resolve() çağırdığı için RLock kullanıyoruz
        self.lock = threading.RLock()
        self.code = None
        self.host_token = None
        self.host_name = "Hakem"
        self.host_last_seen = 0.0
        self.players = {0: None, 1: None}
        self.created_at = None

    # ------------------------------------------------------------------
    @property
    def open(self):
        """Oda henüz oluşturulmadıysa açık mod (tek kullanıcı / testler)."""
        return self.code is None

    def _new_code(self):
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))

    def create(self, name="Hakem"):
        with self.lock:
            self.code = self._new_code()
            self.host_token = secrets.token_urlsafe(24)
            self.host_name = _clean_name(name, "Hakem")
            self.host_last_seen = time.time()
            self.players = {0: None, 1: None}
            self.created_at = time.time()
            return {
                "code": self.code,
                "token": self.host_token,
                "role": "host",
                "name": self.host_name,
            }

    def join(self, code, side, name):
        """Odaya katıl. Dönüş: (bilgi, hata)."""
        side = (side or "").strip().lower()
        if side not in ("blue", "red"):
            return None, "Taraf seçin: mavi (blue) veya kırmızı (red)."
        role = 0 if side == "blue" else 1
        with self.lock:
            if self.code is None:
                return None, "Henüz oda oluşturulmadı. Önce hakem oda oluşturmalı."
            if (code or "").strip().upper() != self.code:
                return None, "Oda kodu hatalı."
            existing = self.players[role]
            if existing and existing.connected():
                return None, f"{ROLE_LABELS[role]} tarafı dolu (başka bir oyuncu bağlı)."
            # Yeni token: eski/ kopuk oturumu geçersiz kılar
            token = secrets.token_urlsafe(24)
            p = Player(role, name, token)
            self.players[role] = p
            return {
                "code": self.code,
                "token": token,
                "role": role,
                "side": side,
                "name": p.name,
            }, None

    # ------------------------------------------------------------------
    def resolve(self, token):
        """Token'ı çöz: (kind, role, player).

        kind: 'host' | 'player' | 'spectator'
        Açık modda (oda yok) herkes host sayılır.
        """
        now = time.time()
        with self.lock:
            if self.code is None:
                return "host", None, None
            if token and self.host_token and secrets.compare_digest(token, self.host_token):
                self.host_last_seen = now
                return "host", None, None
            for role, p in self.players.items():
                if p and token and secrets.compare_digest(token, p.token):
                    p.last_seen = now
                    return "player", role, p
            return "spectator", None, None

    def host_connected(self):
        if not self.host_last_seen:
            return False
        return (time.time() - self.host_last_seen) < CONNECT_TIMEOUT

    # ------------------------------------------------------------------
    def set_ready(self, role, ready):
        with self.lock:
            p = self.players.get(role)
            if p:
                p.ready = bool(ready)
                return p.to_dict()
            return None

    def set_code(self, role, code):
        """Oyuncunun kodunu sakla. Kod değişince 'hazır' sıfırlanır."""
        with self.lock:
            p = self.players.get(role)
            if not p:
                return False
            p.code = code
            p.code_updated = time.time()
            p.ready = False
            return True

    def get_code(self, role):
        with self.lock:
            p = self.players.get(role)
            return p.code if p else None

    def reset_ready(self):
        with self.lock:
            for p in self.players.values():
                if p:
                    p.ready = False

    def leave(self, token):
        with self.lock:
            if token and self.host_token and secrets.compare_digest(token, self.host_token):
                # Hakem ayrılırsa oda kapanır
                self.code = None
                self.host_token = None
                self.host_last_seen = 0.0
                self.players = {0: None, 1: None}
                return True
            for role, p in self.players.items():
                if p and token and secrets.compare_digest(token, p.token):
                    self.players[role] = None
                    return True
            return False

    def state(self, token=None):
        kind, role, player = self.resolve(token)
        with self.lock:
            return {
                "open": self.code is None,
                "code": self.code,
                "host": {
                    "name": self.host_name,
                    "connected": self.host_connected(),
                },
                "players": [
                    self.players[i].to_dict() if self.players[i] else None
                    for i in (0, 1)
                ],
                "you": {
                    "kind": kind,
                    "role": role,
                    "name": (player.name if player else
                             (self.host_name if kind == "host" else "İzleyici")),
                },
            }


ROOM = RoomManager()
