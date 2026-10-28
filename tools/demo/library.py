"""The demonstration library: two dozen songs in four workspaces, each with generated cover art and the same short
piece of audio, encrypted per song the way Suno serves it (AES-128-CTR under a per-song content key).

Everything it makes is cached in .demo/library, so only the first run pays for ffmpeg and the cover art.
"""
from __future__ import annotations

import hashlib
import math
import random
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageChops, ImageDraw, ImageFilter

USER_ID = '6d1c0f3a-2b7e-4c59-a8e4-91f2d05b7c3e'
HANDLE = 'lunacoast'
EMAIL = 'listener@example.com'

WORKSPACES = [
    ('default', 'My Workspace'),
    ('ws-halloween', 'Halloween 2026'),
    ('ws-persian', 'Persian Nights'),
    ('ws-drafts', 'Drafts'),
]

# Newest first. (title, workspace, style tags, model, length in seconds, cover style, palette, extras)
SONGS = [
    ('Glass Harbor', 'default', 'dream pop, shimmering guitars, female vocals', 'v6', 0, 'orbs',
     ['#0b1d3a', '#3d8bfd', '#9be7ff', '#f2f7ff'], {'status': 'streaming'}),
    ('Midnight Static', 'default', 'synthwave, driving bass, 118 bpm', 'v6', 224, 'grid',
     ['#120326', '#ff2a6d', '#ffb86b', '#05d9e8'], {}),
    ('شب‌های تهران', 'default', 'persian pop, darbuka, warm synths', 'v6', 241, 'sun',
     ['#2a0f3d', '#d9485f', '#f9a03f', '#ffe3a3'], {}),
    ('Paper Lanterns', 'default', 'indie folk, fingerpicked guitar, soft harmonies', 'v5', 198, 'orbs',
     ['#2b1406', '#f28c28', '#ffd28a', '#fff1d6'], {'media_error': 403}),
    ('Wolves at the Door', 'default', 'cinematic, taiko drums, epic', 'v6', 187, 'moon',
     ['#070b14', '#24324d', '#a7b4cc', '#e8ecf5'], {'handle': 'coldharbor'}),
    ('Neon Cathedral', 'default', 'darkwave, cathedral reverb, choir pads', 'v6', 263, 'rings',
     ['#0d0221', '#7b2cbf', '#e0aaff', '#ff6bd6'], {}),
    ('Saltwater Hymn', 'default', 'ambient, field recordings, felt piano', 'v5', 276, 'waves',
     ['#03141c', '#0f5e6e', '#5ec2b7', '#d8f3ee'], {}),
    ('Velvet Undertow', 'default', 'neo-soul, rhodes, brushed drums', 'v6', 213, 'sun',
     ['#1a0b12', '#8c1c3a', '#e05a47', '#f7c59f'], {}),
    ('Copper Skyline', 'default', 'lo-fi hip hop, vinyl crackle, 82 bpm', 'v4.5', 162, 'grid',
     ['#1b1209', '#b8622e', '#f0a35e', '#ffe0b8'], {}),
    ('Last Train to Lisbon', 'default', 'bossa nova, nylon guitar, male vocals', 'v5', 205, 'sun',
     ['#0f2235', '#2f6f8f', '#f4b860', '#fde7c2'], {}),
    ('Afterglow (Extended Mix)', 'default', 'deep house, 122 bpm, vocal chops', 'v6', 338, 'orbs',
     ['#16051f', '#c2185b', '#ff8a65', '#ffd180'], {}),
    ('Satellite Hearts', 'default', 'electropop, bright synths, 128 bpm', 'v5', 189, 'rings',
     ['#071a2b', '#00b4d8', '#90e0ef', '#ff70a6'], {}),
    ('Night Market', 'default', 'city pop, slap bass, 1980s', 'v6', 231, 'grid',
     ['#1a0933', '#ff4d6d', '#ffd166', '#06d6a0'], {}),
    ('Quiet Machines', 'default', 'IDM, glitch, granular textures', 'v4.5', 254, 'waves',
     ['#0c0c0c', '#3a3a3a', '#9cff57', '#e6ffd6'], {}),
    ('دریا', 'default', 'persian indie, oud, ambient', 'v6', 219, 'waves',
     ['#041c2c', '#14507a', '#58a4b0', '#e9f5f2'], {}),
    ('Gold Rush Blues', 'default', 'delta blues, slide guitar, stomp', 'v5', 196, 'sun',
     ['#1c1206', '#7a4a12', '#e3a72f', '#fbe7b5'], {}),
    ('Northern Lights Waltz', 'default', 'waltz, orchestral, music box', 'v6', 182, 'moon',
     ['#04121c', '#0b6e4f', '#64f0b4', '#d5fff0'], {}),
    ('Paper Moon Radio', 'default', 'swing, big band, crooner', 'v4.5', 171, 'rings',
     ['#1d1414', '#a4161a', '#e5c07b', '#fff3d6'], {}),
    ('Tidal Memory', 'default', 'chillwave, washed out, tape saturation', 'v5', 247, 'waves',
     ['#120d2b', '#6a4c93', '#f7a1c4', '#ffe5ec'], {}),
    ('Runaway Signal', 'default', 'drum and bass, 174 bpm, reese bass', 'v6', 268, 'grid',
     ['#00121f', '#0077b6', '#ff006e', '#fb5607'], {}),
    ('Lantern Festival', 'default', 'c-pop, guzheng, uplifting', 'v6', 208, 'orbs',
     ['#2a0800', '#d62828', '#f77f00', '#fcbf49'], {}),
    ('Ember & Ash', 'default', 'post-rock, swelling guitars, 6/8', 'v5', 312, 'sun',
     ['#160b0b', '#5e1f1f', '#e85d04', '#ffba08'], {}),
    ('Harbor Lights', 'default', 'acoustic pop, ukulele, whistling', 'v4.5', 158, 'orbs',
     ['#032b2f', '#1b998b', '#f4d35e', '#fffbea'], {}),
    ('Static Bloom', 'default', 'shoegaze, wall of fuzz, dreamy', 'v5', 285, 'rings',
     ['#1a1423', '#5d3a6e', '#e2a0ff', '#ffe8ff'], {}),
    ('Hollow Moon', 'ws-halloween', 'gothic rock, baritone vocals, organ', 'v6', 229, 'moon',
     ['#05030a', '#3c1f5c', '#d4c5f9', '#fdfcff'], {}),
    ('Pumpkin Patch Lullaby', 'ws-halloween', 'music box, lullaby, haunting', 'v6', 147, 'sun',
     ['#120700', '#7f2d00', '#ff7b00', '#ffd29d'], {}),
    ('Graveyard Shift', 'ws-halloween', 'phonk, cowbell, distorted 808', 'v6', 176, 'grid',
     ['#050505', '#3d0066', '#b5ff00', '#f1ffcc'], {}),
    ('Candle Wax & Whispers', 'ws-halloween', 'dark folk, choir, cello', 'v5', 238, 'orbs',
     ['#0e0603', '#5a2a0c', '#f6a440', '#ffe8c2'], {}),
    ('Witching Hour', 'ws-halloween', 'darkwave, 120 bpm, analog synths', 'v6', 252, 'rings',
     ['#0a0012', '#4b0082', '#ff4fd8', '#ffd1f5'], {}),
    ('Bone Orchard', 'ws-halloween', 'industrial, horror, metallic percussion', 'v5', 201, 'moon',
     ['#0b0b0b', '#2e2e2e', '#c9c9c9', '#ff3b3b'], {}),
    ('Moonlit Séance', 'ws-halloween', 'dream pop, reverb, female vocals', 'v6', 219, 'waves',
     ['#0a0f24', '#3b4a8c', '#b8c4ff', '#fff6d6'], {}),
    ('Fog Over Salem', 'ws-halloween', 'cinematic, strings, slow build', 'v6', 194, 'moon',
     ['#0d1214', '#40575e', '#b7c9cc', '#f0f5f6'], {}),
    ('کوچه‌های قدیمی', 'ws-persian', 'persian traditional, tar, tombak', 'v6', 263, 'sun',
     ['#1f0d06', '#8b3a1a', '#e2a65c', '#fbe9d0'], {}),
    ('باران پاییزی', 'ws-persian', 'persian ballad, santur, strings', 'v6', 246, 'waves',
     ['#0c1621', '#3e5c76', '#c3a995', '#f4ede4'], {}),
    ('ستاره‌ی دور', 'ws-persian', 'persian pop, synth pads, male vocals', 'v5', 212, 'orbs',
     ['#0a0a23', '#3f37c9', '#f72585', '#ffd6ec'], {}),
    ('نیمه‌شب', 'ws-persian', 'persian rap, trap beat, dark', 'v6', 188, 'grid',
     ['#0b0012', '#5a189a', '#ff9e00', '#ffe1b3'], {}),
    ('Saffron Dusk', 'ws-persian', 'world fusion, kamancheh, ambient', 'v6', 271, 'sun',
     ['#1d0b00', '#b5451b', '#f2b134', '#fff0c9'], {}),
]


@dataclass
class Song:
    id: str
    title: str
    workspace: str
    tags: str
    model: str
    seconds: int
    style: str
    palette: list[str]
    created: datetime
    status: str = 'complete'
    handle: str = HANDLE
    media_error: int | None = None
    key: bytes = field(default=b'', repr=False)
    iv: bytes = field(default=b'', repr=False)

    @property
    def user_id(self) -> str:
        return USER_ID if self.handle == HANDLE else str(uuid.uuid5(uuid.NAMESPACE_URL, f'suno-user:{self.handle}'))

    def clip(self) -> dict:
        """The song as /api/feed/v3 returns it (the fields the extension reads, plus a few for realism)."""
        clip = {
            'id': self.id,
            'entity_type': 'song_schema',
            'title': self.title,
            'status': self.status,
            'user_id': self.user_id,
            'handle': self.handle,
            'display_name': self.handle,
            'display_tags': self.tags,
            'major_model_version': self.model,
            'created_at': self.created.isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'image_url': f'https://cdn2.suno.ai/image_{self.id}.jpeg',
            'image_large_url': f'https://cdn2.suno.ai/image_large_{self.id}.jpeg',
            'is_public': False,
            'is_trashed': False,
            'metadata': {'tags': self.tags, 'type': 'gen'},
            'media_urls': [],
        }
        if self.status == 'complete':
            clip['metadata']['duration'] = float(self.seconds)
            clip['media_urls'] = [{'url': f'https://cdn1.suno.ai/1/clip/{self.id}.m4a', 'content_type': 'audio/mp4',
                                   'delivery': 'progressive'}]
        return clip


def songs(now: datetime | None = None) -> list[Song]:
    now = now or datetime.now(timezone.utc)
    out = []
    for i, (title, ws, tags, model, seconds, style, palette, extra) in enumerate(SONGS):
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f'suno-demo:{title}'))
        created = now - timedelta(hours=3 + i * 19, minutes=(i * 37) % 60)
        song = Song(sid, title, ws, tags, model, seconds, style, palette, created, **extra)
        song.key = hashlib.sha256(f'content-key:{sid}'.encode()).digest()[:16]
        song.iv = hashlib.sha256(f'base-iv:{sid}'.encode()).digest()[:16]
        out.append(song)
    return out


# ── cover art ──
def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(size, top, bottom):
    img = Image.new('RGB', (1, size))
    for y in range(size):
        img.putpixel((0, y), _mix(top, bottom, y / (size - 1)))
    return img.resize((size, size))


def _grain(img, amount=18, seed=0):
    noise = Image.effect_noise(img.size, amount).convert('RGB')
    return ImageChops.overlay(img, Image.blend(Image.new('RGB', img.size, (128, 128, 128)), noise, 0.35))


def cover(song: Song, size: int = 512) -> Image.Image:
    rnd = random.Random(song.id)
    c = [_rgb(h) for h in song.palette]
    s = size
    if song.style == 'sun':
        img = _gradient(s, c[0], _mix(c[1], c[2], 0.6))
        d = ImageDraw.Draw(img)
        r = s * rnd.uniform(0.18, 0.26)
        cx, cy = s * rnd.uniform(0.35, 0.65), s * rnd.uniform(0.42, 0.55)
        glow = Image.new('RGB', img.size, (0, 0, 0))
        ImageDraw.Draw(glow).ellipse((cx - r * 1.8, cy - r * 1.8, cx + r * 1.8, cy + r * 1.8), fill=c[2])
        img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(s / 9)))
        d = ImageDraw.Draw(img)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c[3])
        for layer in range(4):
            base = s * (0.58 + layer * 0.11)
            amp, freq, phase = s * rnd.uniform(0.03, 0.07), rnd.uniform(1.2, 2.6), rnd.uniform(0, 6.3)
            shade = _mix(c[1], c[0], 0.35 + layer * 0.2)
            pts = [(x, base + amp * math.sin(freq * x / s * 6.28 + phase)) for x in range(0, s + 8, 8)]
            d.polygon(pts + [(s, s), (0, s)], fill=shade)
    elif song.style == 'moon':
        img = _gradient(s, c[0], c[1])
        d = ImageDraw.Draw(img)
        for _ in range(140):
            x, y, b = rnd.uniform(0, s), rnd.uniform(0, s * 0.7), rnd.uniform(0.6, 2.2)
            d.ellipse((x - b, y - b, x + b, y + b), fill=_mix(c[2], c[3], rnd.random()))
        r, cx, cy = s * 0.2, s * rnd.uniform(0.55, 0.7), s * rnd.uniform(0.25, 0.35)
        glow = Image.new('RGB', img.size, (0, 0, 0))
        ImageDraw.Draw(glow).ellipse((cx - r * 1.6, cy - r * 1.6, cx + r * 1.6, cy + r * 1.6), fill=_mix(c[2], c[0], 0.5))
        img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(s / 12)))
        d = ImageDraw.Draw(img)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c[3])
        off = r * 0.45
        d.ellipse((cx - r + off, cy - r - off * 0.4, cx + r + off, cy + r - off * 0.4), fill=_mix(c[0], c[1], 0.35))
        fog = Image.new('RGB', img.size, (0, 0, 0))
        fd = ImageDraw.Draw(fog)
        for i in range(3):
            y = s * (0.72 + i * 0.09)
            fd.ellipse((-s * 0.3, y - s * 0.05, s * 1.3, y + s * 0.05), fill=_mix(c[1], c[2], 0.4))
        img = ImageChops.screen(img, fog.filter(ImageFilter.GaussianBlur(s / 25)))
        d = ImageDraw.Draw(img)
        pts = [(x, s * 0.86 + s * 0.04 * math.sin(x / s * 9 + 1)) for x in range(0, s + 8, 8)]
        d.polygon(pts + [(s, s), (0, s)], fill=c[0])
    elif song.style == 'orbs':
        img = Image.new('RGB', (s, s), c[0])
        layer = Image.new('RGB', (s, s), (0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i in range(7):
            r = s * rnd.uniform(0.14, 0.34)
            x, y = rnd.uniform(0, s), rnd.uniform(0, s)
            d.ellipse((x - r, y - r, x + r, y + r), fill=c[1 + i % 3])
        img = ImageChops.screen(img, layer.filter(ImageFilter.GaussianBlur(s / 14)))
        d = ImageDraw.Draw(img)
        for _ in range(9):
            r = s * rnd.uniform(0.01, 0.03)
            x, y = rnd.uniform(0, s), rnd.uniform(0, s)
            d.ellipse((x - r, y - r, x + r, y + r), outline=c[3], width=2)
    elif song.style == 'grid':
        img = _gradient(s, c[0], c[1])
        d = ImageDraw.Draw(img)
        r, cx, cy = s * 0.24, s / 2, s * 0.5
        sun = Image.new('RGB', img.size, (0, 0, 0))
        sd = ImageDraw.Draw(sun)
        sd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=c[2])
        for i in range(6):
            y = cy + r * (0.15 + i * 0.15)
            sd.rectangle((0, y, s, y + 2 + i * 1.6), fill=(0, 0, 0))
        img = ImageChops.add(img, sun)
        d = ImageDraw.Draw(img)
        horizon = s * 0.62
        d.rectangle((0, horizon, s, s), fill=c[0])
        for i in range(-12, 13):
            d.line((cx + i * s * 0.04, horizon, cx + i * s * 0.32, s), fill=c[3], width=2)
        y, step = horizon, s * 0.012
        while y < s:
            d.line((0, y, s, y), fill=c[3], width=2)
            step *= 1.35
            y += step
    elif song.style == 'waves':
        img = _gradient(s, c[0], c[1])
        d = ImageDraw.Draw(img)
        for band in range(9):
            base = s * (0.25 + band * 0.085)
            amp, freq, phase = s * rnd.uniform(0.02, 0.06), rnd.uniform(1.5, 3.5), rnd.uniform(0, 6.3)
            pts = [(x, base + amp * math.sin(freq * x / s * 6.28 + phase)) for x in range(0, s + 6, 6)]
            d.polygon(pts + [(s, s), (0, s)], fill=_mix(c[1], c[2] if band % 2 else c[3], 0.25 + band * 0.07))
    else:  # rings
        img = _gradient(s, c[0], _mix(c[0], c[1], 0.6))
        d = ImageDraw.Draw(img)
        cx, cy = s * rnd.uniform(0.38, 0.62), s * rnd.uniform(0.38, 0.62)
        for i in range(22, 0, -1):
            r = s * 0.03 * i
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=_mix(c[1], c[2] if i % 3 else c[3], i / 22), width=3)
        glow = Image.new('RGB', img.size, (0, 0, 0))
        ImageDraw.Draw(glow).ellipse((cx - s * 0.12, cy - s * 0.12, cx + s * 0.12, cy + s * 0.12), fill=c[3])
        img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(s / 18)))
    return _grain(img)


# ── audio ──
def audio(folder: Path) -> Path:
    """Twenty seconds of a soft chord progression, as Opus in MP4 like Suno's progressive files."""
    path = folder / 'song.m4a'
    if not path.exists():
        chords = [(220.0, 277.18, 329.63), (196.0, 246.94, 293.66), (174.61, 220.0, 261.63), (196.0, 246.94, 329.63)]
        expr = '+'.join(
            f"between(t,{i * 5},{i * 5 + 5})*0.09*(sin(2*PI*{a}*t)+sin(2*PI*{b}*t)+sin(2*PI*{c}*t))*exp(-0.35*(t-{i * 5}))"
            for i, (a, b, c) in enumerate(chords))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f"aevalsrc='{expr}|{expr}':s=48000:d=20",
                        '-c:a', 'libopus', '-b:a', '96k', '-f', 'mp4', str(path)], check=True)
    return path


def encrypt(song: Song, plain: bytes) -> bytes:
    enc = Cipher(algorithms.AES(song.key), modes.CTR(song.iv)).encryptor()
    return enc.update(plain) + enc.finalize()


def build(folder: Path, now: datetime | None = None) -> list[Song]:
    """Cover art and encrypted media for every song, made once and kept in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    plain = audio(folder).read_bytes()
    library = songs(now)
    for song in library:
        art = folder / f'{song.id}.jpeg'
        if not art.exists():
            cover(song).save(art, quality=90)
        media = folder / f'{song.id}.m4a.enc'
        if song.status == 'complete' and not media.exists():
            media.write_bytes(encrypt(song, plain))
    return library
