#!/usr/bin/env python3
"""Small-team public web app for Fashion Creative Director.

Secrets stay server-side. Visitors authenticate with one shared invite code.
Projects are scoped to an opaque signed browser session.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from core import Engine, Conflict, CATEGORIES, STEMS

APP = Path(__file__).resolve().parent
COOKIE_NAME = 'fcd_session'
SESSION_SECONDS = 30 * 24 * 60 * 60


def load_env():
    """Optional local .env support. Hosted secrets should use provider env vars."""
    path = APP / '.env'
    if not path.exists():
        return
    allowed = {
        'OPENAI_API_KEY', 'FCD_INVITE_CODE', 'FCD_SESSION_SECRET',
        'FCD_TEXT_MODEL', 'FCD_IMAGE_MODEL', 'FCD_LANDSCAPE_SIZE',
        'FCD_PORTRAIT_SIZE', 'FCD_IMAGE_QUALITY', 'FCD_PUBLIC_ORIGIN',
        'FCD_DATA_DIR'
    }
    for raw in path.read_text(encoding='utf-8').splitlines():
        if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw:
            continue
        key, value = raw.split('=', 1)
        key = key.strip(); value = value.strip().strip('"').strip("'")
        if key in allowed:
            os.environ.setdefault(key, value)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def session_secret() -> bytes:
    value = os.environ.get('FCD_SESSION_SECRET', '')
    if len(value) < 32:
        raise RuntimeError('FCD_SESSION_SECRET must be configured with at least 32 characters')
    return value.encode('utf-8')


def issue_session() -> str:
    payload = {
        'sid': secrets.token_urlsafe(24),
        'exp': int(time.time()) + SESSION_SECONDS,
    }
    body = _b64e(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    sig = _b64e(hmac.new(session_secret(), body.encode('ascii'), hashlib.sha256).digest())
    return body + '.' + sig


def verify_session(token: str | None) -> dict | None:
    if not token or '.' not in token:
        return None
    try:
        body, sig = token.rsplit('.', 1)
        expected = _b64e(hmac.new(session_secret(), body.encode('ascii'), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(_b64d(body))
        if int(data.get('exp', 0)) < int(time.time()) or not data.get('sid'):
            return None
        return data
    except Exception:
        return None


def owner_id(session_data: dict) -> str:
    return hashlib.sha256(str(session_data['sid']).encode('utf-8')).hexdigest()


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'FashionCreativeDirectorCloud/1.0'

        def log_message(self, fmt, *args):
            print('[web] ' + (fmt % args), flush=True)

        def respond(self, code, body=b'', kind='application/json; charset=utf-8', filename=None, headers=None):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def output(self, code, data, headers=None):
            self.respond(code, json.dumps(data, ensure_ascii=False).encode('utf-8'), headers=headers)

        def trusted(self, mutation=False):
            host = self.headers.get('Host', '')
            if not host:
                raise PermissionError('Missing host')
            origin = self.headers.get('Origin')
            configured = os.environ.get('FCD_PUBLIC_ORIGIN', '').rstrip('/')
            if origin:
                allowed = {configured} if configured else {'https://' + host, 'http://' + host}
                if origin.rstrip('/') not in allowed:
                    raise PermissionError('Cross-origin requests are not accepted')
            if mutation and self.headers.get('X-FCD-Request') != '1':
                raise PermissionError('Missing request guard')

        def session_data(self):
            raw = self.headers.get('Cookie', '')
            jar = cookies.SimpleCookie()
            try:
                jar.load(raw)
            except cookies.CookieError:
                return None
            morsel = jar.get(COOKIE_NAME)
            return verify_session(morsel.value if morsel else None)

        def require_session(self):
            data = self.session_data()
            if not data:
                raise PermissionError('초대코드 로그인이 필요합니다')
            return data

        def set_session_header(self, token: str) -> str:
            secure = self.headers.get('X-Forwarded-Proto', '').lower() == 'https'
            parts = [f'{COOKIE_NAME}={token}', 'Path=/', f'Max-Age={SESSION_SECONDS}', 'HttpOnly', 'SameSite=Lax']
            if secure:
                parts.append('Secure')
            return '; '.join(parts)

        def clear_session_header(self) -> str:
            return f'{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax'

        def do_GET(self):
            try:
                self.trusted()
                split = urlsplit(self.path); path = split.path
                if path == '/health':
                    self.respond(200, b'OK', 'text/plain; charset=utf-8'); return
                static = {'/': 'index.html', '/app.js': 'app.js', '/app.css': 'app.css'}
                if path in static:
                    p = APP / 'web' / static[path]
                    kind = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}[p.suffix[1:]]
                    self.respond(200, p.read_bytes(), kind); return
                if path == '/api/config':
                    auth = bool(self.session_data())
                    self.output(200, {
                        'authenticated': auth,
                        'mode': 'shared-cloud',
                        'api_key_configured': bool(os.environ.get('OPENAI_API_KEY')),
                        'invite_configured': bool(os.environ.get('FCD_INVITE_CODE')),
                        'image_model': os.environ.get('FCD_IMAGE_MODEL', 'gpt-image-2.5-sunburst'),
                    }); return
                session = self.require_session(); owner = owner_id(session)
                if path == '/api/projects':
                    self.output(200, engine.list_projects(owner)); return
                parts = path.strip('/').split('/')
                if len(parts) >= 3 and parts[:2] == ['api', 'projects']:
                    pid = parts[2]; s = engine.get(pid, owner)
                    if len(parts) == 3:
                        self.output(200, s); return
                    if len(parts) == 5 and parts[3] == 'images' and parts[4] in CATEGORIES:
                        a = s['assets'][parts[4]]; file = engine.root / a['path']
                        kind = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[file.suffix]
                        name = STEMS[parts[4]] + file.suffix if split.query == 'download=1' else None
                        self.respond(200, file.read_bytes(), kind, name); return
                    if len(parts) == 4 and parts[3] == 'bundle' and s['phase'] == 'COMPLETE':
                        self.respond(200, (engine.root / s['package']['path']).read_bytes(), 'application/zip', 'FASHION_CREATIVE_DIRECTOR_FINAL.zip'); return
                self.output(404, {'error': 'Not found'})
            except PermissionError as exc:
                self.output(401, {'error': str(exc)})
            except (ValueError, FileNotFoundError, KeyError):
                self.output(404, {'error': 'Project or asset not found'})

        def do_POST(self):
            try:
                self.trusted(True)
                if not self.headers.get('Content-Type', '').startswith('application/json'):
                    raise ValueError('JSON required')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 22 * 1024 * 1024:
                    raise ValueError('Request exceeds upload limit')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                path = urlsplit(self.path).path

                if path == '/api/login':
                    expected = os.environ.get('FCD_INVITE_CODE', '')
                    supplied = str(body.get('invite_code', '')).strip()
                    if not expected:
                        self.output(503, {'error': '서버에 초대코드가 아직 설정되지 않았습니다'}); return
                    if not hmac.compare_digest(supplied, expected):
                        self.output(401, {'error': '초대코드가 맞지 않습니다'}); return
                    token = issue_session()
                    self.output(200, {'authenticated': True}, {'Set-Cookie': self.set_session_header(token)}); return

                if path == '/api/logout':
                    self.output(200, {'authenticated': False}, {'Set-Cookie': self.clear_session_header()}); return

                session = self.require_session(); owner = owner_id(session)
                if not os.environ.get('OPENAI_API_KEY'):
                    self.output(503, {'error': '서버의 OpenAI API 키가 아직 설정되지 않았습니다'}); return
                if path == '/api/projects':
                    self.output(201, engine.create(body, owner)); return
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'projects'] and parts[3] == 'actions':
                    self.output(200, engine.apply(parts[2], body, owner)); return
                self.output(404, {'error': 'Unknown endpoint'})
            except Conflict as exc:
                self.output(409, {'error': str(exc)})
            except PermissionError as exc:
                self.output(401, {'error': str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.output(400, {'error': str(exc)})
            except FileNotFoundError:
                self.output(404, {'error': 'Project not found'})
            except Exception as exc:
                print('[server-error]', type(exc).__name__, str(exc)[:300], flush=True)
                self.output(500, {'error': 'Server error; no automatic paid retry was sent'})
    return Handler


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '10000')))
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('FCD_DATA_DIR', str(APP / 'data'))))
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()

    session_secret()
    if not os.environ.get('FCD_INVITE_CODE'):
        raise SystemExit('FCD_INVITE_CODE is required')

    engine = Engine(args.data_dir, APP)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine))
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('Fashion Creative Director Cloud listening on port ' + str(server.server_port), flush=True)
    print('OpenAI key stays server-side; visitors authenticate with the invite code.', flush=True)
    if args.open:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); engine.close()


if __name__ == '__main__':
    main()
