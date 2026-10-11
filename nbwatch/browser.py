import os
import shutil
import subprocess

from .config import PROJECT_ROOT
from .util import log, polite_sleep

# Marcadores de páginas de desafío anti-bot (DataDome, Cloudflare, Akamai, Amazon).
BLOCK_MARKERS = (
    "captcha-delivery.com",
    "challenge-platform",
    "cf-turnstile",
    "Ci siamo quasi",
    "Just a moment",
    "px-captcha",
    "Access Denied",
    "api-services-support@amazon.com",
)


def looks_blocked(html: str) -> str | None:
    for marker in BLOCK_MARKERS:
        if marker in html:
            return marker
    return None


class Browser:
    """Chromium "visible" (headful) con perfil persistente; arranca Xvfb si no hay DISPLAY.

    En headless, DataDome (Trovaprezzi) bloquea; headful bajo Xvfb pasa. El perfil persistente
    conserva las cookies del anti-bot entre corridas, lo que reduce los desafíos.
    """

    def __init__(self, profile_dir, headless=False):
        self.profile_dir = str(profile_dir)
        self.headless = headless
        self._xvfb = None
        self._pw = None
        self.ctx = None
        self._pages_loaded = 0

    def __enter__(self):
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and (PROJECT_ROOT / "ms-playwright").is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PROJECT_ROOT / "ms-playwright")
        if not self.headless and not os.environ.get("DISPLAY"):
            self._start_xvfb()
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chromium",
            headless=self.headless,
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        return self

    def __exit__(self, *exc):
        for closer in (lambda: self.ctx and self.ctx.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception as e:  # cerrar nunca debe tapar el error original
                log.debug("error cerrando navegador: %s", e)
        if self._xvfb:
            self._xvfb.terminate()
            try:
                self._xvfb.wait(10)
            except subprocess.TimeoutExpired:
                self._xvfb.kill()

    def _start_xvfb(self):
        if not shutil.which("Xvfb"):
            raise RuntimeError("no hay DISPLAY y no está instalado Xvfb (apt install xvfb)")
        r, w = os.pipe()
        self._xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
            pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(w)
        with os.fdopen(r) as f:
            num = f.readline().strip()
        if not num:
            raise RuntimeError("Xvfb no arrancó")
        os.environ["DISPLAY"] = f":{num}"
        log.debug("Xvfb en DISPLAY=:%s", num)

    def get(self, url, ready_selector, timeout_s=30):
        """Carga url y espera ready_selector (da tiempo a que el desafío JS del anti-bot se resuelva).

        Devuelve (html, url_final, listo). listo=False si el selector no apareció a tiempo.
        """
        if self._pages_loaded:
            polite_sleep(5, 11)
        self._pages_loaded += 1
        page = self.ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_selector(ready_selector, state="attached", timeout=timeout_s * 1000)
                ready = True
            except Exception:
                ready = False
            page.wait_for_timeout(1500)
            return page.content(), page.url, ready
        finally:
            page.close()
