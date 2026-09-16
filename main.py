"""
decky-screen-colors — backend.

Screen color adjustment (saturation, vibrance, temperature, OLED look, ...) for
handhelds running gamescope (Steam Deck and similar).

How it works: the classic X11-atom path is dead on modern (Wayland) gamescope.
This generates a 3D LUT (.cube) and loads it through gamescope's Wayland control
socket:
    XDG_RUNTIME_DIR=/run/user/<uid> WAYLAND_DISPLAY=gamescope-0 \
        gamescopectl set_look <file>   (needs root)

License: GNU GPL v3 or later. The color engine (transform / build_cube /
GamescopeColorBackend) is derived from panel-de-control by Hooandee (GPL-3.0);
see CREDITS.md. This file is distributed under the same license.
"""

import asyncio
import glob
import json
import math
import os
import subprocess
import tempfile

import decky  # provee logger y rutas (DECKY_PLUGIN_SETTINGS_DIR, etc.)

# ---------------------------------------------------------------------------
# Modelo de color: campos y su valor neutro (identidad = look nativo del panel)
# ---------------------------------------------------------------------------
NATIVE = {
    "saturation": 100,   # 0 = gris, 100 = neutro, 200 = muy vívido
    "temperature": 0,    # -100 frío .. +100 cálido
    "contrast": 0,
    "gamma": 0,          # -100 más oscuro .. +100 más claro
    "hue": 0,
    "black": 0,          # punto de negro
    "gain_r": 100,       # ganancias por canal: 100 = 1.0
    "gain_g": 100,
    "gain_b": 100,
    "vibrance": 0,       # como saturación pero respeta los píxeles ya vívidos
}

RANGES = {
    "saturation": (0, 200),
    "temperature": (-100, 100),
    "contrast": (-60, 60),   # acotado para no aplastar el panel a gris ilegible
    "gamma": (-100, 100),
    "hue": (-100, 100),
    "black": (-100, 100),
    "gain_r": (50, 150),
    "gain_g": (50, 150),
    "gain_b": (50, 150),
    "vibrance": (-100, 100),
}

# Presets "look completo" para paneles LCD (empuje firme). Cada uno ajusta
# saturación + calibración estética juntas. Adaptados de panel-de-control.
PRESETS = {
    "cine":   {"saturation": 115, "temperature": 12, "contrast": 18, "gamma": -5,
               "vibrance": 15, "black": -12},
    "vivo":   {"saturation": 145, "contrast": 20, "vibrance": 40, "black": -8},
    "comodo": {"saturation": 95, "temperature": 35, "contrast": -5, "gamma": 5, "black": 5},
}

# Look OLED de un toque: acerca el color de un LCD al de un OLED (más vibrante,
# gamma algo más punchy). NO da negros reales por píxel, solo aproxima el color.
OLED_LOOK = {"saturation": 120, "temperature": 0, "contrast": 20}

# ---------------------------------------------------------------------------
# Motor de color puro: transforma (r,g,b) y genera el LUT .cube
# ---------------------------------------------------------------------------
_LUT_SIZE = 17
_LR, _LG, _LB = 0.2126, 0.7152, 0.0722   # pesos de luma Rec.709
_TEMP_GAIN = 0.3
_HUE_MAX_DEG = 30.0
_BLACK_MAX = 0.15


def _clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _toward_luma(r, g, b, f):
    y = _LR * r + _LG * g + _LB * b
    return y + f * (r - y), y + f * (g - y), y + f * (b - y)


def _black_pt(v, k):
    return k + (1.0 - k) * v if k >= 0.0 else (v + k) / (1.0 + k)


def _gpow(v, exp):
    return v if v <= 0.0 else v ** exp


def _hue_matrix(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (
        (0.213 + c * 0.787 - s * 0.213, 0.715 - c * 0.715 - s * 0.715, 0.072 - c * 0.072 + s * 0.928),
        (0.213 - c * 0.213 + s * 0.143, 0.715 + c * 0.285 + s * 0.140, 0.072 - c * 0.072 - s * 0.283),
        (0.213 - c * 0.213 - s * 0.787, 0.715 - c * 0.715 + s * 0.715, 0.072 + c * 0.928 + s * 0.072),
    )


def _coeffs(state):
    gm, h, bl = state.get("gamma", 0), state.get("hue", 0), state.get("black", 0)
    return (
        state.get("gain_r", 100) / 100.0,
        state.get("gain_g", 100) / 100.0,
        state.get("gain_b", 100) / 100.0,
        state.get("temperature", 0) / 100.0,
        (2.0 ** (-gm / 100.0)) if gm else None,
        state.get("saturation", 100) / 100.0,
        state.get("vibrance", 0) / 100.0,
        _hue_matrix(h * _HUE_MAX_DEG / 100.0) if h else None,
        1.0 + state.get("contrast", 0) / 100.0,
        (bl / 100.0 * _BLACK_MAX) if bl else None,
    )


def _apply(r, g, b, c):
    gr, gg, gb, t, gexp, s, v, hmat, k, kb = c
    r, g, b = r * gr, g * gg, b * gb
    r *= 1.0 + _TEMP_GAIN * t
    b *= 1.0 - _TEMP_GAIN * t
    if gexp is not None:
        r, g, b = _gpow(r, gexp), _gpow(g, gexp), _gpow(b, gexp)
    r, g, b = _toward_luma(r, g, b, s)
    if v:
        sat = max(r, g, b) - min(r, g, b)
        r, g, b = _toward_luma(r, g, b, 1.0 + v * (1.0 - _clamp01(sat)))
    if hmat is not None:
        (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = hmat
        r, g, b = (m00 * r + m01 * g + m02 * b,
                   m10 * r + m11 * g + m12 * b,
                   m20 * r + m21 * g + m22 * b)
    r, g, b = (r - 0.5) * k + 0.5, (g - 0.5) * k + 0.5, (b - 0.5) * k + 0.5
    if kb is not None:
        r, g, b = _black_pt(r, kb), _black_pt(g, kb), _black_pt(b, kb)
    return _clamp01(r), _clamp01(g), _clamp01(b)


def build_cube(state, size=_LUT_SIZE):
    n = size - 1
    c = _coeffs(state)
    lines = ['TITLE "decky-screen-colors"', f"LUT_3D_SIZE {size}"]
    for bi in range(size):
        for gi in range(size):
            for ri in range(size):
                ro, go, bo = _apply(ri / n, gi / n, bi / n, c)
                lines.append(f"{ro:.5f} {go:.5f} {bo:.5f}")
    return "\n".join(lines) + "\n"


def is_native(state):
    return all(state.get(f, v) == v for f, v in NATIVE.items())


# ---------------------------------------------------------------------------
# Capa de dispositivo: lanza gamescopectl contra el socket Wayland de gamescope
# ---------------------------------------------------------------------------
_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _resolve_bin(name):
    for d in _BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return name


def _clean_env(base=None):
    """Env para lanzar binarios del sistema desde el backend congelado de Decky.
    PyInstaller fija LD_LIBRARY_PATH a su bundle, cuyas libs viejas envenenan a los
    binarios del sistema; restauramos el valor previo (LD_LIBRARY_PATH_ORIG) y un
    PATH sano."""
    env = dict(os.environ if base is None else base)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def _run(args, env):
    try:
        argv = [_resolve_bin(args[0]), *args[1:]]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=2,
                           env={**_clean_env(), **env})
        return p.returncode, (p.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


class GamescopeColorBackend:
    """Aplica el color vía `gamescopectl set_look`. Descubre el socket Wayland de
    gamescope en /run/user/*/gamescope-*; queda "no soportado" si no responde.

    El LUT vive dentro de la instancia de gamescope: si gamescope se reinicia
    (reinicio de la consola, reinicio de Steam, cambio a Modo Escritorio y vuelta)
    el look se pierde. Por eso identificamos cada instancia por el inodo de su
    socket y recordamos a cuál le aplicamos el look; `needs_reapply()` avisa
    cuando hay una instancia nueva sin nuestro look puesto."""

    def __init__(self, socket_glob="/run/user/*/gamescope-*", lut_path=None,
                 force_composite=False):
        self._force_composite = force_composite
        self._lut_path = lut_path or os.path.join(tempfile.gettempdir(), "screen_colors_look.cube")
        self._socket_glob = socket_glob
        self._runtime = self._wayland = None
        self._session = None          # instancia de gamescope viva (socket:inodo)
        self._applied_session = None  # instancia a la que ya le cargamos el look
        self._supported = False
        self._probe_detail = "sin probar"
        self._ensure_supported()

    def _discover(self):
        """(XDG_RUNTIME_DIR, WAYLAND_DISPLAY, id de instancia) del primer socket."""
        for sock in sorted(glob.glob(self._socket_glob)):
            try:
                ino = os.stat(sock).st_ino
            except OSError:
                continue
            return os.path.dirname(sock), os.path.basename(sock), "%s:%d" % (sock, ino)
        return None, None, None

    def _ctl(self, *args):
        env = {"XDG_RUNTIME_DIR": self._runtime, "WAYLAND_DISPLAY": self._wayland}
        return _run(["gamescopectl", *args], env)

    def _probe(self):
        rc, _ = self._ctl("version")
        self._probe_detail = f"socket={self._runtime}/{self._wayland} version rc={rc}"
        return rc == 0

    def _ensure_supported(self):
        """Barato de llamar en bucle: solo relanza gamescopectl si cambió el socket."""
        runtime, wayland, session = self._discover()
        if session is None:
            self._supported = False
            self._session = None
            self._probe_detail = f"sin socket gamescope en {self._socket_glob}"
            return False
        if self._supported and session == self._session:
            return True
        self._runtime, self._wayland, self._session = runtime, wayland, session
        self._supported = self._probe()
        if not self._supported:
            self._session = None
        return self._supported

    @property
    def supported(self):
        return self._ensure_supported()

    @property
    def probe_detail(self):
        return self._probe_detail

    def needs_reapply(self, state):
        """True si hay un gamescope vivo que todavía no tiene este look cargado."""
        if is_native(state):
            return False
        if not self._ensure_supported():
            return False
        return self._applied_session != self._session

    def apply(self, state):
        """Escribe el LUT de `state` y lo carga con set_look. Un estado nativo carga
        el LUT identidad para limpiar un look previo. Nunca lanza excepción."""
        if not self._ensure_supported():
            return False
        native = is_native(state)
        if native and self._applied_session is None:
            return True
        try:
            if self._force_composite:
                self._ctl("composite_force", "0" if native else "1")
            with open(self._lut_path, "w") as f:
                f.write(build_cube(state))
            rc, _ = self._ctl("set_look", self._lut_path)
            if rc == 0:
                self._applied_session = None if native else self._session
            return rc == 0
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Persistencia simple (un único perfil global en JSON)
# ---------------------------------------------------------------------------
def _clamp(field, value):
    lo, hi = RANGES[field]
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return NATIVE[field]


def sanitize(fields):
    if not isinstance(fields, dict):
        return {}
    return {f: _clamp(f, fields[f]) for f in NATIVE if f in fields}


def _settings_path():
    base = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR") \
        or os.environ.get("DECKY_PLUGIN_RUNTIME_DIR") \
        or os.path.expanduser("~/.config/decky-screen-colors")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    return os.path.join(base, "color.json")


def _load_state():
    try:
        with open(_settings_path()) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    state = dict(NATIVE)
    state.update(sanitize(raw))
    return state


def _save_state(state):
    try:
        with open(_settings_path(), "w") as f:
            json.dump({f: state[f] for f in NATIVE}, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Plugin de Decky
# ---------------------------------------------------------------------------
def _active_key(state):
    """Qué botón está 'puesto' ahora: nativo, look OLED, un preset o personalizado."""
    if is_native(state):
        return "native"
    for key, values in (("oled", OLED_LOOK), *PRESETS.items()):
        full = dict(NATIVE)
        full.update(values)
        if all(state.get(f) == full[f] for f in NATIVE):
            return key
    return "custom"


class Plugin:

    # Cada cuántos segundos comprobamos que gamescope siga teniendo nuestro look.
    _WATCH_PERIOD = 5

    def _state_dict(self):
        return {
            "supported": self._backend.supported,
            "detail": self._backend.probe_detail,
            "color": {f: self._state[f] for f in NATIVE},
            "ranges": RANGES,
            "native": NATIVE,
            "presets": list(PRESETS.keys()),
            "active": _active_key(self._state),
        }

    async def get_state(self) -> dict:
        return self._state_dict()

    async def _apply_and_save(self):
        # El LUT es 17^3 nodos en Python puro; lo generamos y cargamos en un hilo
        # para no bloquear el event loop mientras se arrastra un slider.
        await asyncio.to_thread(self._backend.apply, dict(self._state))
        await asyncio.to_thread(_save_state, dict(self._state))
        return self._state_dict()

    async def set_color(self, fields: dict) -> dict:
        """Fusiona los campos indicados (ya saneados), aplica y guarda."""
        for f, v in sanitize(fields).items():
            self._state[f] = v
        return await self._apply_and_save()

    async def apply_preset(self, key: str) -> dict:
        """Aplica un look completo (cine/vivo/comodo) o 'native' (reset)."""
        if key == "native":
            self._state = dict(NATIVE)
        elif key in PRESETS:
            self._state = dict(NATIVE)
            self._state.update(sanitize(PRESETS[key]))
        return await self._apply_and_save()

    async def apply_oled_look(self) -> dict:
        """Un toque: acerca el color al de un OLED (más vibrante + contraste)."""
        self._state = dict(NATIVE)
        self._state.update(sanitize(OLED_LOOK))
        return await self._apply_and_save()

    async def reset(self) -> dict:
        self._state = dict(NATIVE)
        return await self._apply_and_save()

    async def _watchdog(self):
        """El LUT muere con la instancia de gamescope (reinicio de la consola o de
        Steam) y Decky puede cargarnos antes de que gamescope esté listo. Este bucle
        detecta una instancia nueva sin nuestro look y lo vuelve a cargar."""
        while True:
            try:
                await asyncio.sleep(self._WATCH_PERIOD)
                if await asyncio.to_thread(self._backend.needs_reapply, dict(self._state)):
                    ok = await asyncio.to_thread(self._backend.apply, dict(self._state))
                    decky.logger.info("Screen Colors: look reaplicado a gamescope (ok=%s)", ok)
            except asyncio.CancelledError:
                raise
            except Exception:
                decky.logger.exception("Screen Colors: fallo en el watchdog")

    async def _main(self):
        self._state = _load_state()
        # force_composite se deja en False: Steam Deck es AMD y lleva el LUT por el
        # pipeline de color del hardware. En Intel/Xe habría que ponerlo en True.
        self._backend = GamescopeColorBackend(force_composite=False)
        decky.logger.info("Screen Colors cargado. Soportado=%s (%s)",
                          self._backend.supported, self._backend.probe_detail)
        # Reaplica el color guardado al arrancar (sobrevive a reinicios).
        if not is_native(self._state):
            await self._apply_and_save()
        self._watch_task = asyncio.create_task(self._watchdog())

    async def _unload(self):
        task = getattr(self, "_watch_task", None)
        if task is not None:
            task.cancel()
        decky.logger.info("Screen Colors descargado")

    async def _uninstall(self):
        # Al desinstalar, vuelve el panel a nativo para no dejar un look colgado.
        try:
            self._backend.apply(dict(NATIVE))
        except Exception:
            pass
