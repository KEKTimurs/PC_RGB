"""
RGB Wave — переливающаяся подсветка в стиле ROG Aura (синий -> фиолетовый -> красный)
для всех устройств через OpenRGB. Живёт в трее, OpenRGB запускает сам в фоне.
"""
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw
from openrgb import OpenRGBClient
from openrgb.utils import DeviceType, RGBColor

APP = "RGBWave"
FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "rgbwave.log")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

DEFAULTS = {
    "colors": ["#0000FF", "#8000FF", "#FF0000"],
    "speed": 3,
    "brightness": 100,
    "enabled": True,
    "fps": 30,
    "wave_length": 1.0,
    "argb_leds": 0,
    "openrgb_path": "",
    "host": "127.0.0.1",
    "port": 6742,
}
# скорость 1..5 -> полных циклов палитры в секунду
SPEEDS = {1: 0.04, 2: 0.08, 3: 0.15, 4: 0.3, 5: 0.6}

logging.basicConfig(
    filename=LOG_PATH, filemode="w", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(APP)


# ---------------------------------------------------------------- конфиг
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg)
    except Exception as e:
        log.error("Ошибка чтения config.json, беру настройки по умолчанию: %s", e)
    cfg["speed"] = min(5, max(1, int(cfg["speed"])))
    cfg["brightness"] = min(100, max(0, int(cfg["brightness"])))
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Не удалось сохранить config.json: %s", e)


# ---------------------------------------------------------------- палитра
def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def build_loop(colors):
    """Синий->фиолет->красный->фиолет->синий: замкнутый градиент без скачка."""
    rgb = [hex_to_rgb(c) for c in colors]
    return rgb + rgb[-2:0:-1] if len(rgb) > 2 else rgb


def sample(loop, pos):
    n = len(loop)
    x = (pos % 1.0) * n
    i = int(x) % n
    f = x - int(x)
    a, b = loop[i], loop[(i + 1) % n]
    return tuple(a[k] + (b[k] - a[k]) * f for k in range(3))


# ---------------------------------------------------------------- OpenRGB
def find_openrgb(cfg):
    candidates = [
        cfg.get("openrgb_path") or "",
        os.path.join(BASE, "OpenRGB", "OpenRGB.exe"),
        os.path.join(BASE, "OpenRGB.exe"),
        r"C:\Program Files\OpenRGB\OpenRGB.exe",
        r"C:\OpenRGB\OpenRGB.exe",
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def connect(cfg, stop):
    try:
        return OpenRGBClient(cfg["host"], cfg["port"], name=APP)
    except Exception:
        pass
    exe = find_openrgb(cfg)
    if not exe:
        raise RuntimeError("OpenRGB.exe не найден. Положите папку OpenRGB рядом с RGBWave.exe "
                           "или укажите путь в config.json (openrgb_path).")
    log.info("Запускаю OpenRGB: %s", exe)
    subprocess.Popen([exe, "--server", "--startminimized"], cwd=os.path.dirname(exe))
    for _ in range(60):
        if stop.is_set():
            raise SystemExit
        time.sleep(1)
        try:
            return OpenRGBClient(cfg["host"], cfg["port"], name=APP)
        except Exception:
            continue
    raise RuntimeError("Не удалось подключиться к OpenRGB за 60 секунд.")


def wait_devices(client, stop):
    """Ждём, пока OpenRGB закончит поиск устройств (число перестанет меняться)."""
    last, stable = -1, 0
    for _ in range(40):
        if stop.is_set():
            return
        client.update()
        n = len(client.devices)
        stable = stable + 1 if (n == last and n > 0) else 0
        if stable >= 3:
            return
        last = n
        time.sleep(1)


def prepare(client, cfg):
    argb = int(cfg.get("argb_leds") or 0)
    for dev in client.devices:
        if argb > 0 and dev.type == DeviceType.MOTHERBOARD:
            for z in dev.zones:
                lo = getattr(z, "leds_min", None)
                hi = getattr(z, "leds_max", None)
                if lo is not None and hi is not None:
                    resizable = hi > lo
                    size = min(max(argb, lo), hi)
                else:
                    resizable = any(k in z.name.upper() for k in ("ARGB", "RAINBOW"))
                    size = argb
                if resizable and len(z.leds) != size:
                    try:
                        z.resize(size)
                        log.info("Зона %s: %d LED", z.name, size)
                    except Exception as e:
                        log.warning("Не удалось изменить размер зоны %s: %s", z.name, e)
    client.update()
    for dev in client.devices:
        try:
            dev.set_custom_mode()
        except Exception as e:
            log.warning("%s: не удалось включить Direct режим: %s", dev.name, e)
        log.info("Устройство: %s [%s], LED: %d, зоны: %s", dev.name, dev.type.name,
                 len(dev.leds), ", ".join(f"{z.name}({len(z.leds)})" for z in dev.zones))


# ---------------------------------------------------------------- анимация
class Animator(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                client = connect(self.app.cfg, self.stop)
                wait_devices(client, self.stop)
                prepare(client, self.app.cfg)
                if not client.devices:
                    self.app.notify("OpenRGB не нашёл ни одного устройства. Смотрите rgbwave.log")
                self.loop(client)
            except SystemExit:
                return
            except Exception as e:
                log.exception("Ошибка, переподключение через 5 с")
                self.app.notify(str(e)[:200])
                self.stop.wait(5)

    def loop(self, client):
        phase = 0.0
        last = time.perf_counter()
        was_on = None
        while not self.stop.is_set():
            cfg = self.app.cfg
            frame = 1.0 / max(5, int(cfg["fps"]))
            now = time.perf_counter()
            dt, last = now - last, now
            on = cfg["enabled"]
            if on:
                phase = (phase + dt * SPEEDS[cfg["speed"]]) % 1.0
                loop = build_loop(cfg["colors"])
                k = cfg["brightness"] / 100.0
                wl = float(cfg["wave_length"])
                for dev in client.devices:
                    n = len(dev.leds)
                    if not n:
                        continue
                    cols = []
                    for i in range(n):
                        r, g, b = sample(loop, phase + i / n * wl)
                        cols.append(RGBColor(int(r * k), int(g * k), int(b * k)))
                    dev.set_colors(cols, fast=True)
            elif was_on is not False:
                for dev in client.devices:
                    if dev.leds:
                        dev.set_color(RGBColor(0, 0, 0), fast=True)
            was_on = on
            self.stop.wait(max(0.0, frame - (time.perf_counter() - now)))


# ---------------------------------------------------------------- автозапуск
def autostart_enabled():
    r = subprocess.run(["schtasks", "/query", "/tn", APP], capture_output=True,
                       creationflags=NO_WINDOW)
    return r.returncode == 0


def set_autostart(on):
    if on:
        if not FROZEN:
            log.warning("Автозапуск доступен только для собранного RGBWave.exe")
            return
        cmd = (f'schtasks /create /tn {APP} /tr "\\"{sys.executable}\\"" '
               f'/sc onlogon /rl highest /f')
    else:
        cmd = f"schtasks /delete /tn {APP} /f"
    r = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    log.info("Автозапуск %s: код %d", "вкл" if on else "выкл", r.returncode)


# ---------------------------------------------------------------- трей
def make_icon():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    loop = build_loop(DEFAULTS["colors"])
    for x in range(size):
        r, g, b = sample(loop, x / size / 2)
        d.line([(x, 0), (x, size)], fill=(int(r), int(g), int(b), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((2, 2, size - 2, size - 2), fill=255)
    img.putalpha(mask)
    return img


class App:
    def __init__(self):
        self.cfg = load_config()
        self.autostart = autostart_enabled()
        self.anim = Animator(self)
        self.icon = pystray.Icon(APP, make_icon(), "RGB Wave", self.menu())

    def notify(self, msg):
        try:
            self.icon.notify(msg, "RGB Wave")
        except Exception:
            pass

    def update(self, **kw):
        self.cfg.update(kw)
        save_config(self.cfg)

    def menu(self):
        def speed_item(s):
            return pystray.MenuItem(str(s), lambda icon, item: self.update(speed=s),
                                    checked=lambda item: self.cfg["speed"] == s, radio=True)

        def bright_item(b):
            return pystray.MenuItem(f"{b}%", lambda icon, item: self.update(brightness=b),
                                    checked=lambda item: self.cfg["brightness"] == b, radio=True)

        return pystray.Menu(
            pystray.MenuItem("Подсветка включена",
                             lambda icon, item: self.update(enabled=not self.cfg["enabled"]),
                             checked=lambda item: self.cfg["enabled"], default=True),
            pystray.MenuItem("Скорость", pystray.Menu(*[speed_item(s) for s in range(1, 6)])),
            pystray.MenuItem("Яркость", pystray.Menu(*[bright_item(b) for b in (25, 50, 75, 100)])),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Запускать вместе с Windows", self.toggle_autostart,
                             checked=lambda item: self.autostart),
            pystray.MenuItem("Открыть config.json", lambda icon, item: os.startfile(CONFIG_PATH)),
            pystray.MenuItem("Перечитать config.json", self.reload),
            pystray.MenuItem("Открыть лог", lambda icon, item: os.startfile(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    def toggle_autostart(self, icon, item):
        set_autostart(not self.autostart)
        self.autostart = autostart_enabled()

    def reload(self, icon, item):
        self.cfg = load_config()
        self.notify("Настройки перечитаны")

    def quit(self, icon, item):
        self.anim.stop.set()
        icon.stop()

    def run(self):
        self.anim.start()
        self.icon.run()


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\RGBWave_single_instance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)
    App().run()
