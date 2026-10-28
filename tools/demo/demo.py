"""The README's screenshots, from the real extension: a copy of this working tree (so a UI change shows at once) loaded
unpacked into Playwright's Chromium, with its side panel opened as a tab and suno.com replaced by a stand-in that serves
a demonstration library (tools/demo/suno.py). Downloads really run: license, encrypted file, decryption, chrome.downloads.
Everything it keeps is in .demo/ (git ignores it); the browser profile and the downloads start empty on every run.

    python tools/demo/demo.py                     retake every screenshot of tools/demo/shots.json in docs/screenshots
    python tools/demo/demo.py --only 01-hero      only these shots (comma-separated names)
    python tools/demo/demo.py serve               open the demo in a visible Chromium until you close it; the
                                                  extension's toolbar icon opens the real side panel
    python tools/demo/demo.py reset               forget .demo/: the copy, the library, the profile, the downloads

One thing is changed for the pictures: chrome.downloads reports files under .demo/Downloads, a path that names this
computer's user, so the side panel is shown C:\\Users\\you\\Downloads in its place.

Needs ffmpeg, Pillow, cryptography and Playwright for Python
(pip install playwright pillow cryptography; python -m playwright install chromium).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import sync_playwright

sys.dont_write_bytecode = True  # no __pycache__ inside the extension's folder

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import capture  # noqa: E402
import library  # noqa: E402
import suno  # noqa: E402

STATE = ROOT / '.demo'
CODE = STATE / 'code'
LIBRARY = STATE / 'library'
PROFILE = STATE / 'profile'
DOWNLOADS = STATE / 'Downloads'
SHOWN_DOWNLOADS = 'C:\\Users\\you\\Downloads'
NOT_COPIED = ('docs/', 'tools/', '.')

# capture.py resolves shot paths with urljoin, which leaves schemes it does not know alone.
for _registry in (urllib.parse.uses_relative, urllib.parse.uses_netloc):
    _registry.append('chrome-extension')

SHOW_DOWNLOADS_JS = """(() => {
  if (location.protocol !== 'chrome-extension:' || !globalThis.chrome?.downloads) return;
  const search = chrome.downloads.search.bind(chrome.downloads);
  const from = %s, to = %s;
  chrome.downloads.search = async (query) => (await search(query)).map((item) =>
    item.filename?.startsWith(from) ? { ...item, filename: to + item.filename.slice(from.length) } : item);
})();"""


def say(text: str) -> None:
    print(f'[demo] {text}', flush=True)


# ── the copy ──
def sync_code() -> None:
    """Brings .demo/code up to date with the working tree: the extension's own files, without docs/ and tools/."""
    listed = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard'], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.splitlines()
    wanted = {p for p in listed if not p.startswith(NOT_COPIED) and (ROOT / p).is_file()}
    changed = 0
    for rel in wanted:
        src, dst = ROOT / rel, CODE / rel
        if not dst.exists() or dst.stat().st_size != src.stat().st_size or dst.stat().st_mtime < src.stat().st_mtime:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            changed += 1
    for path in sorted(CODE.rglob('*'), reverse=True):
        rel = path.relative_to(CODE).as_posix()
        if path.is_file() and rel not in wanted:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    say(f'code: {len(wanted)} files, {changed} updated')


def extension_id(path: Path) -> str:
    """Chrome's id for an unpacked extension: SHA-256 of its path (UTF-16, drive letter upper case), as a-p."""
    norm = str(path.resolve())
    norm = norm[0].upper() + norm[1:]
    return ''.join(chr(ord('a') + int(c, 16)) for c in hashlib.sha256(norm.encode('utf-16-le')).hexdigest()[:32])


def fresh_profile() -> None:
    """An empty browser profile whose downloads go to .demo/Downloads, never to this computer's Downloads folder."""
    for folder in (PROFILE, DOWNLOADS):
        shutil.rmtree(folder, ignore_errors=True)
    (PROFILE / 'Default').mkdir(parents=True)
    DOWNLOADS.mkdir(parents=True)
    prefs = {'download': {'default_directory': str(DOWNLOADS), 'prompt_for_download': False, 'directory_upgrade': True},
             'savefile': {'default_directory': str(DOWNLOADS)}}
    (PROFILE / 'Default' / 'Preferences').write_text(json.dumps(prefs), encoding='utf-8')


# ── the browser ──
def launch(playwright, headless: bool, opts: dict, stand_in: suno.Suno):
    context = playwright.chromium.launch_persistent_context(
        str(PROFILE), channel='chromium', headless=headless,
        args=[f'--disable-extensions-except={CODE}', f'--load-extension={CODE}'],
        ignore_default_args=['--disable-extensions'],  # Playwright's default would switch the extension off
        **{k: v for k, v in opts.items() if k in ('viewport', 'no_viewport', 'device_scale_factor', 'color_scheme', 'reduced_motion')})
    context.add_init_script(SHOW_DOWNLOADS_JS % (json.dumps(str(DOWNLOADS)), json.dumps(SHOWN_DOWNLOADS)))
    stand_in.attach(context)
    tab = context.pages[0] if context.pages else context.new_page()
    # Playwright names every download by a random id; Chrome's own handling keeps the name the extension asks for.
    context.new_cdp_session(tab).send('Browser.setDownloadBehavior', {'behavior': 'default'})
    tab.goto('https://suno.com/create')
    return context


class ExtensionBrowser:
    """What capture.py takes for a browser: its one context is the persistent one the extension is loaded into."""

    def __init__(self, playwright, headless: bool, stand_in: suno.Suno):
        self.playwright, self.headless, self.stand_in = playwright, headless, stand_in
        self.context = None

    def new_context(self, **opts):
        if self.context is None:
            self.context = launch(self.playwright, self.headless, opts, self.stand_in)
        return self.context

    def close(self) -> None:
        if self.context is not None:
            self.context.close()


def prepare() -> tuple[suno.Suno, str]:
    STATE.mkdir(exist_ok=True)
    sync_code()
    songs = library.build(LIBRARY)
    say(f'library: {len(songs)} songs in {len(library.WORKSPACES)} workspaces')
    fresh_profile()
    return suno.Suno(songs, LIBRARY), extension_id(CODE)


def shoot(only: str | None, headed: bool) -> int:
    stand_in, ext = prepare()
    spec = (HERE / 'shots.json').read_text(encoding='utf-8').replace('{extension}', ext)
    resolved = STATE / 'shots.json'
    resolved.write_text(spec, encoding='utf-8')

    @contextmanager
    def playwright_with_extension():
        with sync_playwright() as p:
            yield SimpleNamespace(chromium=SimpleNamespace(
                launch=lambda headless=True: ExtensionBrowser(p, headless, stand_in)))

    capture.sync_playwright = playwright_with_extension
    sys.argv = ['capture.py', str(resolved), '--out', str(ROOT / 'docs' / 'screenshots')]
    sys.argv += ['--only', only] if only else []
    sys.argv += ['--headed'] if headed else []
    try:
        capture.main()
    except SystemExit as done:
        return done.code or 0
    return 0


def serve() -> None:
    stand_in, ext = prepare()
    with sync_playwright() as p:
        context = launch(p, False, {'no_viewport': True}, stand_in)
        panel = context.new_page()
        panel.goto(f'chrome-extension://{ext}/sidepanel.html')
        say('running: the side panel is open in its own tab, and the toolbar icon opens it as a real side panel. '
            'Close the browser to stop.')
        try:
            context.wait_for_event('close', timeout=0)
        except KeyboardInterrupt:
            context.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', nargs='?', choices=['shoot', 'serve', 'reset'], default='shoot')
    ap.add_argument('--only', help='comma-separated shot names')
    ap.add_argument('--headed', action='store_true', help='show the browser while shooting')
    args = ap.parse_args()
    if args.command == 'reset':
        shutil.rmtree(STATE, ignore_errors=True)
        say('forgot .demo/')
    elif args.command == 'serve':
        serve()
    else:
        sys.exit(shoot(args.only, args.headed))


if __name__ == '__main__':
    main()
