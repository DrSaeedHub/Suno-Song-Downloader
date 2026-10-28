#!/usr/bin/env python3
"""Capture README screenshots from a JSON shot list with Playwright (Chromium).

    python capture.py SHOTS.json [--out DIR] [--only NAME,NAME] [--headed]

The shot-list format is in ../references/screenshots.md. Shots run in order in
one browser session, so a login done in "setup" carries over to every shot.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import struct
import sys
from pathlib import Path
from urllib.parse import urljoin

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright for Python is missing: pip install playwright && python -m playwright install chromium")

README_WIDTH = 840  # CSS px of the README column on a desktop github.com repository page
PHONE = {"width": 390, "height": 844}
MAX_HEIGHT = 1100       # CSS px: one screen, a 1080p TV included; anything taller is a page-long capture
MAX_PHONE_HEIGHT = 900  # CSS px: one phone screen

BASE_CSS = """
*, *::before, *::after { caret-color: transparent !important; }
::-webkit-scrollbar { display: none !important; }
html { scrollbar-width: none !important; }
"""
FREEZE_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important;
  transition-duration: 0s !important; transition-delay: 0s !important;
}
"""
# Style tags vanish on navigation, so the CSS is re-added on every page load.
STYLE_JS = """(() => {
  const css = %s;
  const add = () => {
    const s = document.createElement('style');
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();"""

SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]|\x1b\][^\x07]*\x07")
PALETTE = ["#484f58", "#ff7b72", "#3fb950", "#d29922", "#58a6ff", "#bc8cff", "#39c5cf", "#b1bac4",
           "#6e7681", "#ffa198", "#56d364", "#e3b341", "#79c0ff", "#d2a8ff", "#56d4dd", "#ffffff"]


def ansi_to_html(text: str) -> str:
    """Colors, bold and dim from SGR codes; other escapes dropped; \\r keeps the last overwrite."""
    text = text.replace("\r\n", "\n")
    text = "\n".join(line.split("\r")[-1] for line in text.split("\n"))
    text = OTHER_ESCAPES.sub("", text)
    out: list[str] = []
    fg, bold, dim = None, False, False

    def emit(chunk: str) -> None:
        if not chunk:
            return
        style = [f"color:{fg}"] if fg else []
        if bold:
            style.append("font-weight:700")
        if dim:
            style.append("opacity:.6")
        escaped = html.escape(chunk)
        out.append(f'<span style="{";".join(style)}">{escaped}</span>' if style else escaped)

    pos = 0
    for m in SGR.finditer(text):
        emit(text[pos:m.start()])
        pos = m.end()
        codes = [int(c) if c else 0 for c in m.group(1).split(";")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                fg, bold, dim = None, False, False
            elif c == 1:
                bold = True
            elif c == 2:
                dim = True
            elif c == 22:
                bold = dim = False
            elif c == 39:
                fg = None
            elif 30 <= c <= 37:
                fg = PALETTE[c - 30]
            elif 90 <= c <= 97:
                fg = PALETTE[c - 90 + 8]
            elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                fg = PALETTE[codes[i + 2]] if codes[i + 2] < 16 else None
                i += 2
            elif c == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
                fg = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                i += 4
            i += 1
    emit(text[pos:])
    return "".join(out)


def terminal_html(text: str, command: str | None, title: str, width: int, prompt: str) -> str:
    head = ""
    if command:
        head = f'<span class="p">{html.escape(prompt)}</span> <span class="c">{html.escape(command)}</span>\n'
    return f"""<!doctype html><meta charset="utf-8"><style>
html, body {{ margin: 0; background: transparent; }}
body {{ padding: 24px; }}
.term {{ width: {width}px; background: #0d1117; border: 1px solid #30363d; border-radius: 10px; overflow: hidden;
  font: 13.5px/1.55 "Cascadia Mono", "JetBrains Mono", Consolas, "DejaVu Sans Mono", monospace; color: #e6edf3; }}
.bar {{ display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: #161b22;
  border-bottom: 1px solid #30363d; }}
.bar i {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block; }}
.bar .t {{ flex: 1; text-align: center; color: #8b949e; font-size: 12px; margin-right: 52px; }}
pre {{ margin: 0; padding: 16px 18px; white-space: pre-wrap; word-break: break-word; font: inherit; }}
.p {{ color: #3fb950; }} .c {{ font-weight: 600; }}
</style><div class="term"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i
style="background:#28c840"></i><span class="t">{html.escape(title)}</span></div><pre>{head}{ansi_to_html(text.rstrip())}</pre></div>"""


def run_actions(page, actions, url) -> None:
    for action in actions or []:
        if len(action) != 1:
            raise ValueError(f"an action has exactly one key: {action}")
        (kind, arg), = action.items()
        if kind == "goto":
            page.goto(url(arg), wait_until="load")
        elif kind in ("click", "dblclick", "hover", "check"):
            getattr(page.locator(arg).first, kind)()
        elif kind == "scroll_to":
            page.locator(arg).first.scroll_into_view_if_needed()
        elif kind == "wait_for":
            page.locator(arg).first.wait_for(state="visible")
        elif kind == "fill":
            page.locator(arg[0]).first.fill(os.path.expandvars(str(arg[1])))
        elif kind == "type":
            page.locator(arg[0]).first.press_sequentially(os.path.expandvars(str(arg[1])))
        elif kind == "press":
            page.locator(arg[0]).first.press(arg[1])
        elif kind == "select":
            page.locator(arg[0]).first.select_option(arg[1])
        elif kind == "key":
            page.keyboard.press(arg)
        elif kind == "wait":
            page.wait_for_timeout(int(arg))
        elif kind == "wait_url":
            page.wait_for_url(arg)
        elif kind == "eval":
            page.evaluate(arg)
        elif kind == "reload":
            page.reload(wait_until="load")
        else:
            raise ValueError(f"unknown action {kind!r}")


class Session:
    def __init__(self, browser, spec: dict, here: Path):
        self.browser, self.spec, self.here = browser, spec, here
        self.scale = spec.get("scale", 2)
        self.viewport = spec.get("viewport", {"width": 1440, "height": 900})
        self.color_scheme = spec.get("color_scheme", "dark")
        self.css = BASE_CSS + (FREEZE_CSS if spec.get("freeze_animations", True) else "") + spec.get("css", "")
        self.contexts: dict[tuple, object] = {}
        self.pages: dict[tuple, object] = {}

    def url(self, path: str) -> str:
        return urljoin(self.spec["base_url"], path) if self.spec.get("base_url") else path

    def page(self, mobile: bool, scale: float | None = None):
        """One browser context per (phone or not, pixel ratio); a new one starts with the first one's login."""
        key = (mobile, scale or self.scale)
        if key not in self.pages:
            opts = dict(viewport=PHONE if mobile else self.viewport, device_scale_factor=key[1],
                        color_scheme=self.color_scheme, reduced_motion="reduce", ignore_https_errors=True,
                        is_mobile=mobile, has_touch=mobile)
            for name, opt in (("locale", "locale"), ("timezone", "timezone_id")):
                if self.spec.get(name):
                    opts[opt] = self.spec[name]
            if self.contexts:
                opts["storage_state"] = next(iter(self.contexts.values())).storage_state()
            elif self.spec.get("storage_state"):
                opts["storage_state"] = str(self.here / self.spec["storage_state"])
            ctx = self.browser.new_context(**opts)
            ctx.set_default_timeout(self.spec.get("timeout", 15000))
            ctx.add_init_script(STYLE_JS % json.dumps(self.css))
            if self.spec.get("init_script"):
                ctx.add_init_script(self.spec["init_script"])
            self.contexts[key] = ctx
            self.pages[key] = ctx.new_page()
        return self.pages[key]

    def setup(self) -> None:
        run_actions(self.page(False), self.spec.get("setup"), self.url)

    def take(self, shot: dict, out: Path) -> Path:
        mobile = bool(shot.get("mobile"))
        page = self.page(mobile, shot.get("scale"))
        if "terminal" in shot:
            return self.terminal(page, shot["terminal"], out / f"{shot['name']}.png")
        fmt = shot.get("format", self.spec.get("format", "png"))
        path = out / f"{shot['name']}.{'jpg' if fmt == 'jpeg' else 'png'}"
        page.set_viewport_size(shot.get("viewport") or (PHONE if mobile else self.viewport))
        page.emulate_media(color_scheme=shot.get("color_scheme", self.color_scheme), reduced_motion="reduce")
        target = shot.get("url") or shot.get("path")
        if target is not None:
            page.goto(self.url(target), wait_until="load")
        run_actions(page, shot.get("actions"), self.url)
        if shot.get("wait_for"):
            page.locator(shot["wait_for"]).first.wait_for(state="visible")
        hide = self.spec.get("hide", []) + shot.get("hide", [])
        if hide:
            page.add_style_tag(content=", ".join(hide) + " { visibility: hidden !important; }")
        page.wait_for_timeout(shot.get("delay", self.spec.get("delay", 600)))

        # Python writes the files: Playwright's own writer fails on Windows paths over 260 characters.
        opts = {"animations": "disabled", "type": fmt}
        if fmt == "jpeg":
            opts["quality"] = shot.get("quality", self.spec.get("quality", 88))
        if shot.get("selector"):
            loc = page.locator(shot["selector"]).first
            loc.scroll_into_view_if_needed()
            pad = shot.get("padding", 0)
            if pad:
                box = loc.bounding_box()
                sx, sy, doc_w, doc_h = page.evaluate(
                    "[scrollX, scrollY, document.documentElement.scrollWidth, document.documentElement.scrollHeight]")
                x, y = max(0, box["x"] + sx - pad), max(0, box["y"] + sy - pad)
                clip = {"x": x, "y": y,
                        "width": min(doc_w - x, box["width"] + 2 * pad), "height": min(doc_h - y, box["height"] + 2 * pad)}
                data = page.screenshot(clip=clip, full_page=True, **opts)
            else:
                data = loc.screenshot(**opts)
        elif shot.get("clip"):
            data = page.screenshot(clip=shot["clip"], full_page=True, **opts)
        else:
            data = page.screenshot(full_page=bool(shot.get("full_page")), **opts)
        path.write_bytes(data)
        return path

    def terminal(self, page, spec: dict, path: Path) -> Path:
        text = spec.get("text", "")
        if "file" in spec:  # newline="" keeps lone \r, so an overwritten progress line shows only its last state
            with open(self.here / spec["file"], encoding="utf-8", newline="") as f:
                text = f.read()
        width = spec.get("width", 860)
        page.set_viewport_size({"width": width + 48, "height": 600})
        page.set_content(terminal_html(text, spec.get("command"), spec.get("title", ""), width, spec.get("prompt", "$")))
        path.write_bytes(page.locator(".term").screenshot(omit_background=True))
        return path

    def save_failure(self, shot: dict, folder: Path) -> None:
        try:
            folder.mkdir(exist_ok=True)
            page = self.page(bool(shot.get("mobile")), shot.get("scale"))
            (folder / f"{shot['name']}.png").write_bytes(page.screenshot())
        except Exception:
            pass


def optimize(path: Path) -> None:
    if path.suffix != ".png":
        return
    try:
        from PIL import Image
    except ImportError:
        return
    with Image.open(path) as im:
        im.load()
        im.save(path, optimize=True)


def image_size(path: Path) -> tuple[int, int]:
    if path.suffix == ".png":
        with open(path, "rb") as f:
            return struct.unpack(">II", f.read(24)[16:24])
    data = path.read_bytes()  # JPEG: the first start-of-frame marker holds the size
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise ValueError(f"no image size in {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shots", help="the shot list (JSON)")
    ap.add_argument("--out", help="output folder (default: the shot list's \"out\")")
    ap.add_argument("--only", help="comma-separated shot names to capture")
    ap.add_argument("--headed", action="store_true", help="show the browser")
    args = ap.parse_args()

    spec_path = Path(args.shots).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_arg = args.out or spec.get("out")
    if not out_arg:
        sys.exit("no output folder: pass --out or set \"out\" in the shot list")
    out = Path(out_arg).resolve()
    out.mkdir(parents=True, exist_ok=True)
    only = {n.strip() for n in (args.only or "").split(",") if n.strip()}
    shots = [s for s in spec["shots"] if not only or s["name"] in only]
    if only - {s["name"] for s in shots}:
        sys.exit("unknown shot names: " + ", ".join(sorted(only - {s['name'] for s in shots})))

    done, failed = [], []
    failures = spec_path.parent / "_failed"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        session = Session(browser, spec, spec_path.parent)
        try:
            if any("terminal" not in s for s in shots):  # terminal shots need no login
                try:
                    session.setup()
                except Exception as e:
                    session.save_failure({"name": "setup"}, failures)
                    sys.exit(f"FAIL  setup: {str(e).strip().splitlines()[0]}\n      the page at that moment: "
                             f"{failures / 'setup.png'}")
            for shot in shots:
                try:
                    path = session.take(shot, out)
                    optimize(path)
                    done.append((shot, path))
                except Exception as e:  # keep going: one broken shot shouldn't cost the rest
                    failed.append((shot["name"], str(e).strip().splitlines()[0]))
                    session.save_failure(shot, failures)
        finally:
            browser.close()

    for shot, path in list(done):
        scale = shot.get("scale", spec.get("scale", 2))
        w, h = image_size(path)
        # The owner's rule: no screenshot taller than one screen. A page-long capture is deleted, not warned about.
        limit = MAX_PHONE_HEIGHT if shot.get("mobile") else MAX_HEIGHT
        if h / scale > limit:
            path.unlink()
            done.remove((shot, path))
            failed.append((shot["name"], f"{round(h / scale)} CSS px tall, over the {limit} px limit (deleted): crop to "
                                         "the part the text is about, or split it into two shots"))
    for shot, path in done:
        scale = shot.get("scale", spec.get("scale", 2))
        w, h = image_size(path)
        notes = []
        css_w = w / scale
        if css_w > README_WIDTH and not shot.get("mobile") and "terminal" not in shot:
            pct = round(100 * README_WIDTH / css_w)
            if pct < 75:
                notes.append(f"text shows at ~{pct}% of its size in a README: crop to an element or use a narrower "
                             "viewport, unless this is the hero")
        kb = path.stat().st_size // 1024
        if kb > 800:
            notes.append(f"large file ({kb} KB)")
        print(f"ok    {path}  {w}x{h}  {kb} KB" + "".join(f"\n      ! {n}" for n in notes))
    for name, err in failed:
        print(f"FAIL  {name}: {err}")
    if failed:
        print(f"      screenshots of the failing pages: {failures}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
