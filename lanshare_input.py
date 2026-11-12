"""
Injection d'entrées (souris / clavier) pour le contrôle à distance de LanShare.
Aucune dépendance : ctypes uniquement.

  Windows : SendInput / SetCursorPos (user32)
  Linux   : X11 + XTest (libX11, libXtst)  — pas Wayland
  macOS   : non pris en charge pour l'instant
"""
import ctypes, ctypes.util, os, re, subprocess, sys, threading

FKEYS = {f"F{i}": i for i in range(1, 13)}


class InputError(Exception):
    pass


class Backend:
    name = "?"

    def set_monitor(self, w, h):  # choisit l'écran dont la taille correspond à la capture
        pass

    def move(self, nx, ny): ...
    def button(self, b, down): ...
    def wheel(self, dx, dy): ...
    def key(self, down, key, mods): ...
    def text(self, s): ...
    def release_all(self): ...


# --------------------------------------------------------------------------- test
class FakeBackend(Backend):
    """Enregistre les événements (tests / --fake-input)."""
    name = "fake"

    def __init__(self):
        self.log, self.mon = [], None

    def set_monitor(self, w, h): self.mon = (w, h)
    def move(self, nx, ny): self.log.append(("move", round(nx, 4), round(ny, 4)))
    def button(self, b, down): self.log.append(("button", b, down))
    def wheel(self, dx, dy): self.log.append(("wheel", dx, dy))
    def key(self, down, key, mods): self.log.append(("key", down, key, tuple(sorted(k for k, v in mods.items() if v))))
    def text(self, s): self.log.append(("text", s))
    def release_all(self): self.log.append(("release_all",))


# ------------------------------------------------------------------------ Windows
class WindowsBackend(Backend):
    name = "windows"
    VK = {"Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B, "Delete": 0x2E, "Insert": 0x2D,
          "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22, "ArrowLeft": 0x25, "ArrowUp": 0x26,
          "ArrowRight": 0x27, "ArrowDown": 0x28, "Shift": 0x10, "Control": 0x11, "Alt": 0x12, "Meta": 0x5B,
          "CapsLock": 0x14, " ": 0x20, "ContextMenu": 0x5D, "PrintScreen": 0x2C, "Pause": 0x13,
          "NumLock": 0x90, "ScrollLock": 0x91}
    EXT = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D, 0x2C}

    def __init__(self):
        from ctypes import wintypes as w
        self.w, self.u = w, ctypes.windll.user32
        for fn in (lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2), lambda: self.u.SetProcessDPIAware()):
            try:
                fn(); break
            except Exception:
                pass
        ULONG_PTR = ctypes.c_size_t

        class MI(ctypes.Structure):
            _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                        ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KI(ctypes.Structure):
            _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HI(ctypes.Structure):
            _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

        class U(ctypes.Union):
            _fields_ = [("mi", MI), ("ki", KI), ("hi", HI)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", w.DWORD), ("u", U)]
        self.MI, self.KI, self.INPUT = MI, KI, INPUT
        self.rect = (0, 0, self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1))
        self.held_keys, self.held_btn = set(), set()
        self.lock = threading.Lock()

    def _monitors(self):
        w = self.w
        res = []
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]
        PROC = ctypes.WINFUNCTYPE(ctypes.c_int, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)

        def cb(hm, hdc, lprc, lp):
            mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(mi)
            self.u.GetMonitorInfoW(hm, ctypes.byref(mi))
            r = mi.rcMonitor
            res.append(((r.left, r.top, r.right - r.left, r.bottom - r.top), bool(mi.dwFlags & 1)))
            return 1
        self.u.EnumDisplayMonitors(None, None, PROC(cb), 0)
        return res

    def set_monitor(self, w, h):
        mons = self._monitors()
        pick = next((r for r, p in mons if (r[2], r[3]) == (w, h) and p), None) \
            or next((r for r, p in mons if (r[2], r[3]) == (w, h)), None) \
            or next((r for r, p in mons if p), None)
        if pick:
            self.rect = pick

    def _send(self, *inputs):
        arr = (self.INPUT * len(inputs))(*inputs)
        self.u.SendInput(len(inputs), arr, ctypes.sizeof(self.INPUT))

    def _mouse(self, flags, data=0):
        i = self.INPUT(type=0); i.mi = self.MI(0, 0, data & 0xFFFFFFFF, flags, 0, 0); return i

    def _kb(self, vk, up, scan=0, unicode=False):
        f = (2 if up else 0) | (4 if unicode else 0) | (1 if (not unicode and vk in self.EXT) else 0)
        i = self.INPUT(type=1); i.ki = self.KI(0 if unicode else vk, scan, f, 0, 0); return i

    def move(self, nx, ny):
        x, y, w, h = self.rect
        self.u.SetCursorPos(int(x + nx * (w - 1)), int(y + ny * (h - 1)))

    def button(self, b, down):
        flags = {0: (2, 4), 1: (0x20, 0x40), 2: (8, 0x10)}[b]
        self._send(self._mouse(flags[0] if down else flags[1]))
        (self.held_btn.add if down else self.held_btn.discard)(b)

    def wheel(self, dx, dy):
        if dy: self._send(self._mouse(0x0800, int(-dy * 120)))
        if dx: self._send(self._mouse(0x1000, int(dx * 120)))

    def _vk_for(self, key):
        if key in self.VK: return self.VK[key]
        if key in FKEYS: return 0x70 + FKEYS[key] - 1
        if len(key) == 1:
            r = self.u.VkKeyScanW(key)
            if r != -1 and (r & 0xFF) != 0xFF: return r & 0xFF
        return None

    def key(self, down, key, mods):
        if len(key) == 1 and not (mods.get("ctrl") or mods.get("alt") or mods.get("meta")):
            for unit in self._utf16(key):
                self._send(self._kb(0, not down, unit, True))
            return
        vk = self._vk_for(key)
        if vk is None: return
        self._send(self._kb(vk, not down))
        (self.held_keys.add if down else self.held_keys.discard)(vk)

    @staticmethod
    def _utf16(s):
        b = s.encode("utf-16-le")
        return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]

    def text(self, s):
        for ch in s:
            if ch in "\r\n":
                self.key(True, "Enter", {}); self.key(False, "Enter", {}); continue
            for unit in self._utf16(ch):
                self._send(self._kb(0, False, unit, True), self._kb(0, True, unit, True))

    def release_all(self):
        for vk in list(self.held_keys): self._send(self._kb(vk, True))
        for b in list(self.held_btn): self.button(b, False)
        self.held_keys.clear(); self.held_btn.clear()


# --------------------------------------------------------------------------- X11
class X11Backend(Backend):
    name = "x11"
    SYM = {"Enter": 0xff0d, "Backspace": 0xff08, "Tab": 0xff09, "Escape": 0xff1b, "Delete": 0xffff,
           "Home": 0xff50, "ArrowLeft": 0xff51, "ArrowUp": 0xff52, "ArrowRight": 0xff53, "ArrowDown": 0xff54,
           "PageUp": 0xff55, "PageDown": 0xff56, "End": 0xff57, "Insert": 0xff63, "Shift": 0xffe1,
           "Control": 0xffe3, "Alt": 0xffe9, "Meta": 0xffeb, "CapsLock": 0xffe5, " ": 0x20,
           "ContextMenu": 0xff67, "PrintScreen": 0xff61, "Pause": 0xff13, "NumLock": 0xff7f, "ScrollLock": 0xff14}

    def __init__(self):
        if not os.environ.get("DISPLAY"):
            raise InputError("aucun serveur X11 (DISPLAY absent) — Wayland n'est pas pris en charge")
        x11, xt = ctypes.util.find_library("X11"), ctypes.util.find_library("Xtst")
        if not (x11 and xt):
            raise InputError("bibliothèques X11/Xtst introuvables (sudo apt install libxtst6)")
        self.x, self.t = ctypes.CDLL(x11), ctypes.CDLL(xt)
        x, t = self.x, self.t
        x.XOpenDisplay.restype = ctypes.c_void_p; x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte; x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XKeycodeToKeysym.restype = ctypes.c_ulong; x.XKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XDisplayWidth.argtypes = x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        t.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
        t.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        t.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.d = x.XOpenDisplay(None)
        if not self.d:
            raise InputError("impossible d'ouvrir l'écran X11")
        self.rect = (0, 0, x.XDisplayWidth(self.d, 0), x.XDisplayHeight(self.d, 0))
        self.shift_held = False
        self.held_keys, self.held_btn = set(), set()

    def set_monitor(self, w, h):
        mons = []
        try:
            out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=3).stdout
            for m in re.finditer(r"^\S+ connected( primary)? (\d+)x(\d+)\+(\d+)\+(\d+)", out, re.M):
                mons.append(((int(m[4]), int(m[5]), int(m[2]), int(m[3])), bool(m[1])))
        except Exception:
            pass
        pick = next((r for r, p in mons if (r[2], r[3]) == (w, h) and p), None) \
            or next((r for r, p in mons if (r[2], r[3]) == (w, h)), None) \
            or next((r for r, p in mons if p), None)
        if pick: self.rect = pick

    def _flush(self): self.x.XFlush(self.d)

    def move(self, nx, ny):
        x, y, w, h = self.rect
        self.t.XTestFakeMotionEvent(self.d, -1, int(x + nx * (w - 1)), int(y + ny * (h - 1)), 0); self._flush()

    def button(self, b, down):
        self.t.XTestFakeButtonEvent(self.d, {0: 1, 1: 2, 2: 3}[b], 1 if down else 0, 0); self._flush()
        (self.held_btn.add if down else self.held_btn.discard)(b)

    def wheel(self, dx, dy):
        for amount, neg, pos in ((dy, 4, 5), (dx, 6, 7)):
            if amount:
                btn = pos if amount > 0 else neg
                for _ in range(max(1, min(10, round(abs(amount))))):
                    self.t.XTestFakeButtonEvent(self.d, btn, 1, 0); self.t.XTestFakeButtonEvent(self.d, btn, 0, 0)
        self._flush()

    def _sym(self, key):
        if key in self.SYM: return self.SYM[key]
        if key in FKEYS: return 0xffbe + FKEYS[key] - 1
        if len(key) == 1:
            o = ord(key)
            return o if (0x20 <= o <= 0x7e or 0xa0 <= o <= 0xff) else 0x01000000 + o
        return None

    def _press(self, kc, down):
        self.t.XTestFakeKeyEvent(self.d, kc, 1 if down else 0, 0)

    def key(self, down, key, mods):
        sym = self._sym(key)
        if sym is None: return
        kc = self.x.XKeysymToKeycode(self.d, sym)
        if not kc: return
        if key == "Shift": self.shift_held = down
        need_shift = (len(key) == 1 and self.x.XKeycodeToKeysym(self.d, kc, 0) != sym
                      and self.x.XKeycodeToKeysym(self.d, kc, 1) == sym and not self.shift_held)
        shift_kc = self.x.XKeysymToKeycode(self.d, 0xffe1)
        if need_shift and down: self._press(shift_kc, True)
        self._press(kc, down)
        if need_shift and down: self._press(shift_kc, False)
        (self.held_keys.add if down else self.held_keys.discard)(kc)
        self._flush()

    def text(self, s):
        for ch in s:
            k = "Enter" if ch in "\r\n" else ch
            self.key(True, k, {}); self.key(False, k, {})

    def release_all(self):
        for kc in list(self.held_keys): self._press(kc, False)
        for b in list(self.held_btn): self.button(b, False)
        self.held_keys.clear(); self.held_btn.clear(); self.shift_held = False; self._flush()


def make_backend(fake=False):
    """Retourne (backend, raison_si_indisponible)."""
    if fake:
        return FakeBackend(), ""
    try:
        if sys.platform.startswith("win"):
            return WindowsBackend(), ""
        if sys.platform.startswith("linux"):
            return X11Backend(), ""
        return None, "Le contrôle à distance n'est pas encore disponible sur ce système (Windows et Linux/X11 pris en charge)."
    except InputError as e:
        return None, str(e)
    except Exception as e:  # noqa
        return None, f"Contrôle indisponible : {e}"
