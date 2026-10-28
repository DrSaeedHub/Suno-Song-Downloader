"""A stand-in for suno.com, answered from inside the browser with Playwright routes, so the demo never reaches Suno.

It serves what the extension talks to, in the shapes it reads:

    https://suno.com/create                   a placeholder page for the tab whose session the extension uses
    https://suno.com/a/api/project/me         the workspaces
    https://suno.com/a/api/feed/v3            a workspace's songs, 20 to a page
    https://suno.com/a/api/mango/rights       a license: the song's key and IV, AES-256-GCM wrapped under
                                              SHA-256 of the Bearer token, with the clip id as additional data
    https://cdn1.suno.ai/1/clip/<id>.m4a      the encrypted audio (one song answers 403, like an expired link)
    https://cdn2.suno.ai/image_<id>.jpeg      cover art

and a control endpoint the shot list drives with `eval` actions:

    https://suno.com/__demo/hold?title=<t>    keep that song's audio download waiting (for an in-progress shot)
    https://suno.com/__demo/release           let it finish

Every other https:// request is aborted, so nothing leaves the machine.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import library

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Suno (demonstration stand-in)</title>
<body style="margin:0;display:grid;place-items:center;height:100vh;background:#101014;color:#c9c9d1;font:16px system-ui">
<p>suno.com (demonstration stand-in)</p></body></html>"""


def b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=').decode()


def session_token() -> str:
    """A __session JWT with the claims the extension reads; the signature is never checked, so it is a placeholder."""
    claims = {
        'sub': 'user_demo',
        'sid': 'sess_demo',
        'exp': int(time.time()) + 30 * 86400,
        'suno.com/claims/user_id': library.USER_ID,
        'suno.com/claims/email': library.EMAIL,
        'suno/handle': library.HANDLE,
    }
    return f"{b64url({'alg': 'RS256', 'typ': 'JWT'})}.{b64url(claims)}.demo-signature"


class Suno:
    def __init__(self, songs: list[library.Song], media: Path):
        self.songs = songs
        self.by_id = {song.id: song for song in songs}
        self.media = media
        self.hold_title: str | None = None
        self.held = []

    def attach(self, context) -> None:
        context.add_cookies([{'name': '__session', 'value': session_token(), 'domain': 'suno.com', 'path': '/',
                              'secure': True, 'sameSite': 'Lax'}])
        context.route('https://**/*', self.handle)
        context.route('http://**/*', lambda route: route.abort())

    # ── dispatch ──
    def handle(self, route) -> None:
        request = route.request
        url = urlsplit(request.url)
        try:
            if url.hostname in ('suno.com', 'www.suno.com'):
                if url.path.startswith('/a/api/'):
                    return self.api(route, request, url.path[2:], url.query)
                if url.path.startswith('/__demo/'):
                    return self.control(route, url)
                return route.fulfill(body=PAGE, content_type='text/html')
            if url.hostname == 'cdn1.suno.ai':
                return self.audio(route, url.path.rsplit('/', 1)[-1].split('.')[0])
            if url.hostname == 'cdn2.suno.ai':
                song_id = url.path.rsplit('_', 1)[-1].split('.')[0]
                art = self.media / f'{song_id}.jpeg'
                return route.fulfill(path=str(art), content_type='image/jpeg') if art.exists() else route.fulfill(status=404)
            return route.abort()  # nothing else is answered, and nothing goes out
        except Exception as error:  # a broken handler would leave the request hanging; fail it visibly instead
            print(f'[suno] {request.method} {request.url}: {error}', flush=True)
            route.fulfill(status=500, body=str(error))

    def api(self, route, request, path: str, query: str) -> None:
        if path == '/api/project/me':
            page = int(parse_qs(query).get('page', ['1'])[0])
            projects = [{'id': ws, 'name': name, 'clip_count': sum(s.workspace == ws for s in self.songs)}
                        for ws, name in library.WORKSPACES] if page == 1 else []
            return route.fulfill(json={'projects': projects, 'num_total_results': len(library.WORKSPACES)})
        if path == '/api/feed/v3':
            body = json.loads(request.post_data or '{}')
            workspace = body.get('filters', {}).get('workspace', {}).get('workspaceId', 'default')
            clips = [s.clip() for s in self.songs if s.workspace == workspace]
            start, limit = int(body.get('cursor') or 0), int(body.get('limit') or 20)
            more = start + limit < len(clips)
            return route.fulfill(json={'clips': clips[start:start + limit], 'has_more': more,
                                       'next_cursor': str(start + limit) if more else None})
        if path == '/api/mango/rights':
            body = json.loads(request.post_data or '{}')
            song = self.by_id.get(body.get('content_params', {}).get('content_id'))
            token = request.headers.get('authorization', '').removeprefix('Bearer ')
            if not song or not token:
                return route.fulfill(status=404, json={'detail': 'Not found'})
            kek = AESGCM(hashlib.sha256(token.encode()).digest())

            def wrap(secret: bytes) -> str:
                nonce = os.urandom(12)
                return base64.b64encode(nonce + kek.encrypt(nonce, secret, song.id.encode())).decode()

            return route.fulfill(json={'key': wrap(song.key), 'iv': wrap(song.iv)})
        return route.fulfill(status=404, json={'detail': 'Not found'})

    def audio(self, route, song_id: str) -> None:
        song = self.by_id.get(song_id)
        if not song or song.status != 'complete':
            return route.fulfill(status=404, body='Not Found', content_type='text/plain')
        if song.media_error:
            return route.fulfill(status=song.media_error, content_type='application/xml',
                                 body='<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>')
        if self.hold_title == song.title:
            self.held.append((route, song))  # answered by /__demo/release
            return
        self.send_audio(route, song)

    def send_audio(self, route, song) -> None:
        route.fulfill(path=str(self.media / f'{song.id}.m4a.enc'), content_type='audio/mp4')

    def control(self, route, url) -> None:
        command, args = url.path.removeprefix('/__demo/'), parse_qs(url.query)
        if command == 'hold':
            self.hold_title = args['title'][0]
        elif command == 'release':
            self.hold_title = None
            held, self.held = self.held, []
            for waiting, song in held:
                self.send_audio(waiting, song)
        route.fulfill(json={'ok': True, 'held': len(self.held)})
