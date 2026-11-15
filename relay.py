"""
HotSpell Relay + License Server — Railway deployment
Listens on Railway's assigned PORT.
Detects HTTP (no colon in first line) vs relay protocol (HOST:/JOIN:).

HTTP endpoints:
  POST /webhook/payhip   — receives Payhip sale webhooks, stores license key
  POST /validate         — HotSpell calls this to validate a license key
  GET  /admin/add?secret=S&key=K  — manually register a key (admin only)
  GET  /admin/list?secret=S       — list all keys (admin only)

Environment variables:
  PORT                   — Railway assigns this (default 8080)
  DB_PATH                — SQLite path (default /data/licenses.db; mount a Volume at /data)
  ADMIN_SECRET           — secret for /admin/* endpoints
  PAYHIP_WEBHOOK_SECRET  — optional; used to verify Payhip HMAC signatures
"""
import os, socket, threading, secrets, time, sqlite3, json, hmac, hashlib, urllib.parse

PORT            = int(os.environ.get('PORT', 8080))
DB_PATH         = os.environ.get('DB_PATH', '/data/licenses.db')
ADMIN_SECRET    = os.environ.get('ADMIN_SECRET', '')
WEBHOOK_SECRET  = os.environ.get('PAYHIP_WEBHOOK_SECRET', '')

# ── License database ────────────────────────────────────────────────────────

_db      = None
_db_lock = threading.Lock()


def _db_init():
    global _db
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception:
        pass
    try:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute('''CREATE TABLE IF NOT EXISTS licenses
                       (key TEXT PRIMARY KEY, email TEXT, created_at INTEGER)''')
        _db.commit()
        count = _db.execute('SELECT COUNT(*) FROM licenses').fetchone()[0]
        print(f'[license] DB at {DB_PATH} — {count} keys loaded')
    except Exception as e:
        print(f'[license] DB init failed ({DB_PATH}): {e} — falling back to /tmp')
        try:
            _db = sqlite3.connect('/tmp/licenses.db', check_same_thread=False)
            _db.execute('''CREATE TABLE IF NOT EXISTS licenses
                           (key TEXT PRIMARY KEY, email TEXT, created_at INTEGER)''')
            _db.commit()
            print('[license] using /tmp/licenses.db (ephemeral — mount a Railway Volume at /data for persistence)')
        except Exception as e2:
            print(f'[license] fallback DB also failed: {e2}')
            _db = None


def _key_add(key: str, email: str = '') -> bool:
    if _db is None:
        return False
    k = key.strip().upper()
    with _db_lock:
        try:
            _db.execute('INSERT OR IGNORE INTO licenses (key, email, created_at) VALUES (?,?,?)',
                        (k, email, int(time.time())))
            _db.commit()
            print(f'[license] key registered: {k[:8]}*** ({email})')
            return True
        except Exception as e:
            print(f'[license] add error: {e}')
            return False


def _key_valid(key: str) -> bool:
    if _db is None:
        return False
    k = key.strip().upper()
    with _db_lock:
        try:
            row = _db.execute('SELECT key FROM licenses WHERE key=?', (k,)).fetchone()
            return row is not None
        except Exception:
            return False


def _key_list() -> list:
    if _db is None:
        return []
    with _db_lock:
        try:
            rows = _db.execute('SELECT key, email, created_at FROM licenses ORDER BY created_at DESC').fetchall()
            return [{'key': r[0], 'email': r[1], 'ts': r[2]} for r in rows]
        except Exception:
            return []


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_send(conn: socket.socket, status: int, body: bytes,
               content_type: str = 'application/json'):
    status_text = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden',
                   404: 'Not Found', 500: 'Internal Server Error'}.get(status, 'Unknown')
    resp = (f'HTTP/1.1 {status} {status_text}\r\n'
            f'Content-Type: {content_type}\r\n'
            f'Content-Length: {len(body)}\r\n'
            f'Connection: close\r\n\r\n').encode() + body
    try:
        conn.sendall(resp)
    except Exception:
        pass


def _json(conn, status, **kwargs):
    _http_send(conn, status, json.dumps(kwargs).encode())


# ── HTTP request handler ──────────────────────────────────────────────────────

def _handle_http(conn: socket.socket, first_line: str):
    try:
        parts   = first_line.split()
        method  = parts[0].upper() if parts else 'GET'
        path    = parts[1] if len(parts) > 1 else '/'

        # Read headers
        headers = {}
        while True:
            hline = _readline(conn)
            if not hline:
                break
            if ':' in hline:
                k, v = hline.split(':', 1)
                headers[k.strip().lower()] = v.strip()

        # Read body
        body = b''
        cl = int(headers.get('content-length', 0))
        if cl > 0:
            chunks = []
            remaining = min(cl, 65536)
            while remaining > 0:
                chunk = conn.recv(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b''.join(chunks)

        # ── Route ────────────────────────────────────────────────────────────

        # Health check
        if path in ('/', '/health'):
            _http_send(conn, 200, b'HotSpell License Server OK', 'text/plain')
            return

        # POST /webhook/payhip
        if path == '/webhook/payhip' and method == 'POST':
            # Optional signature verification
            if WEBHOOK_SECRET:
                sig_header = headers.get('payhip-signature', '')
                expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(sig_header, expected):
                    _json(conn, 403, error='invalid signature')
                    print('[webhook] bad signature')
                    return

            # Parse payload — Payhip sends form-encoded or JSON
            payload = {}
            ct = headers.get('content-type', '')
            if 'json' in ct:
                try:
                    payload = json.loads(body)
                except Exception:
                    pass
            else:
                try:
                    payload = dict(urllib.parse.parse_qsl(body.decode('utf-8', errors='replace')))
                except Exception:
                    pass

            print(f'[webhook] payload keys: {list(payload.keys())}')
            print(f'[webhook] raw: {body[:500]}')

            # Extract license key — Payhip may use different field names
            lic_key = (payload.get('license_key') or payload.get('licenseKey') or
                       payload.get('key') or payload.get('license') or '')
            email   = payload.get('buyer_email') or payload.get('email') or ''

            if lic_key:
                _key_add(lic_key, email)
                _json(conn, 200, success=True, key=lic_key[:8] + '***')
            else:
                print(f'[webhook] no license_key found in payload')
                _json(conn, 200, success=True, note='no license_key in payload')
            return

        # POST /webhook/email  — Make.com sends raw Gmail body; server extracts key via regex
        if path == '/webhook/email' and method == 'POST':
            import re as _re
            text = body.decode('utf-8', errors='replace')
            qs_params = dict(urllib.parse.parse_qsl(text))
            email_body = qs_params.get('body', text)
            secret = qs_params.get('secret', '')
            if ADMIN_SECRET and secret != ADMIN_SECRET:
                _json(conn, 403, error='forbidden')
                return
            matches = _re.findall(r'[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}', email_body.upper())
            if not matches:
                print(f'[email-webhook] no key found in body (len={len(email_body)})')
                _json(conn, 200, success=False, note='no license key found in email body')
                return
            added = []
            for key in set(matches):
                if _key_add(key):
                    added.append(key)
            print(f'[email-webhook] added {len(added)} key(s): {added}')
            _json(conn, 200, success=True, keys_added=len(added), keys=added)
            return

        # POST /validate   body: key=XXXXX-XXXXX-XXXXX-XXXXX
        if path == '/validate' and method == 'POST':
            params = {}
            try:
                params = dict(urllib.parse.parse_qsl(body.decode('utf-8', errors='replace')))
            except Exception:
                pass
            key = params.get('key', '').strip()
            if not key:
                _json(conn, 400, valid=False, reason='missing key')
                return
            if _key_valid(key):
                _json(conn, 200, valid=True)
            else:
                _json(conn, 200, valid=False, reason='key not found')
            return

        # GET /admin/add?secret=S&key=K
        if path.startswith('/admin/add'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            secret = (qs.get('secret') or [''])[0]
            key    = (qs.get('key') or [''])[0].strip()
            email  = (qs.get('email') or [''])[0].strip()
            if not ADMIN_SECRET or secret != ADMIN_SECRET:
                _json(conn, 403, error='forbidden')
                return
            if not key:
                _json(conn, 400, error='missing key')
                return
            ok = _key_add(key, email)
            _json(conn, 200, success=ok, key=key)
            return

        # GET /admin/list?secret=S
        if path.startswith('/admin/list'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
            secret = (qs.get('secret') or [''])[0]
            if not ADMIN_SECRET or secret != ADMIN_SECRET:
                _json(conn, 403, error='forbidden')
                return
            keys = _key_list()
            _json(conn, 200, count=len(keys), keys=keys)
            return

        _json(conn, 404, error='not found')

    except Exception as e:
        print(f'[http] handler error: {e}')
        try:
            _json(conn, 500, error=str(e))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Relay (game networking) ──────────────────────────────────────────────────

_rooms: dict = {}

# Pool of AI servers waiting for a player.
#
# An AI server sends 'AIHOST:' and waits; a player sends 'PLAYAI:' and is
# paired with the first one free. Deliberately NO room code: the whole point is
# that the player taps "Play against CPU" and does nothing else. Once paired
# this uses the same _pipe plumbing as a human-vs-human room, so the relay
# cannot tell the two apart and neither can the client.
_ai_hosts: list = []
_lock         = threading.Lock()


def _cleanup():
    while True:
        time.sleep(60)
        now = time.monotonic()
        with _lock:
            stale = [c for c, r in _rooms.items()
                     if r['guest'] is None and now - r['ts'] > 600]
            for c in stale:
                try:  _rooms[c]['host'].close()
                except Exception: pass
                del _rooms[c]
                print(f'[relay] expired room {c}')


threading.Thread(target=_cleanup, daemon=True).start()


def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:  s.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try:  s.close()
            except Exception: pass


def _readline(conn: socket.socket) -> str:
    buf = b''
    while len(buf) < 4096:
        try:
            b = conn.recv(1)
        except Exception:
            return ''
        if not b:
            return ''
        buf += b
        if b == b'\n':
            break
    return buf.decode('ascii', errors='replace').strip()


def _set_keepalive(s: socket.socket):
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
    except Exception:
        pass


def _handle(conn: socket.socket):
    _set_keepalive(conn)
    conn.settimeout(15)
    try:
        line = _readline(conn)
        if not line:
            conn.close()
            return

        # HTTP: first line has no colon (e.g. "GET /validate HTTP/1.1")
        if ':' not in line:
            _handle_http(conn, line)
            return

        role, code = line.split(':', 1)
        role = role.upper().strip()
        code = code.upper().strip()[:6]

        if role == 'HOST':
            with _lock:
                if not code or code in _rooms:
                    for _ in range(20):
                        code = secrets.token_hex(3).upper()
                        if code not in _rooms:
                            break
                event = threading.Event()
                _rooms[code] = {
                    'host': conn, 'guest': None,
                    'event': event, 'ts': time.monotonic()
                }
            conn.sendall(f'CODE:{code}\n'.encode())
            print(f'[relay] HOST registered room {code}')

            conn.settimeout(None)
            if not event.wait(timeout=600):
                print(f'[relay] room {code} timed out')
                with _lock: _rooms.pop(code, None)
                conn.close()
                return

            with _lock:
                room = _rooms.pop(code, None)
            if room is None or room['guest'] is None:
                conn.close()
                return

            guest = room['guest']
            conn.sendall(b'GO\n')
            print(f'[relay] room {code} paired')
            t = threading.Thread(target=_pipe, args=(guest, conn), daemon=True)
            t.start()
            _pipe(conn, guest)

        elif role == 'JOIN':
            with _lock:
                room = _rooms.get(code)
                if room is None or room['guest'] is not None:
                    room = None
                else:
                    room['guest'] = conn

            if room is None:
                conn.sendall(b'NOTFOUND\n')
                conn.close()
                print(f'[relay] JOIN code {code} not found')
                return

            conn.settimeout(None)
            conn.sendall(b'GO\n')
            room['event'].set()
            print(f'[relay] guest joined room {code}')

        elif role == 'AIHOST':
            # An AI server offering itself. Waits in the pool until a player
            # asks for a CPU game, then behaves exactly like a HOST.
            entry = {'conn': conn, 'guest': None,
                     'event': threading.Event(), 'ts': time.monotonic()}
            with _lock:
                _ai_hosts.append(entry)
                waiting = len(_ai_hosts)
            conn.sendall(b'AIREADY\n')
            print(f'[relay] AI host registered ({waiting} waiting)')

            conn.settimeout(None)
            if not entry['event'].wait(timeout=3600):
                with _lock:
                    if entry in _ai_hosts:
                        _ai_hosts.remove(entry)
                conn.close()
                return

            guest = entry['guest']
            if guest is None:
                conn.close()
                return
            # 'GO <level>' when the player chose one (PLAYAI:guru), bare 'GO'
            # otherwise - the AI host then keeps its own --ai-level.
            lvl = entry.get('level', '')
            conn.sendall(f'GO {lvl}\n'.encode() if lvl else b'GO\n')
            print('[relay] AI game paired')
            t = threading.Thread(target=_pipe, args=(guest, conn), daemon=True)
            t.start()
            _pipe(conn, guest)

        elif role == 'PLAYAI':
            # A player asking for a CPU opponent. Take the first AI server in
            # the pool; if there is none, say so plainly so the client can tell
            # the user instead of hanging on a connection that will never pair.
            # Optional level: 'PLAYAI:novice' / 'PLAYAI:expert' / 'PLAYAI:guru'.
            # Anything else (including bare 'PLAYAI:') = the host's default.
            level = code.lower() if code.lower() in ('novice', 'expert', 'guru') else ''
            entry = None
            with _lock:
                while _ai_hosts:
                    cand = _ai_hosts.pop(0)
                    if cand['guest'] is None:
                        entry = cand
                        entry['level'] = level
                        entry['guest'] = conn
                        break
            if entry is None:
                conn.sendall(b'NOAI\n')
                conn.close()
                print('[relay] PLAYAI requested but no AI host available')
                return
            conn.settimeout(None)
            conn.sendall(b'GO\n')
            entry['event'].set()
            print(f'[relay] player matched to AI host (level {level or "default"})')
            # NOT closed here: the AIHOST thread pipes this socket, exactly as
            # the HOST thread pipes a JOIN guest. Closing it would kill the game.

        else:
            conn.close()

    except Exception as e:
        print(f'[relay] error: {e}')
        try: conn.close()
        except Exception: pass


def main():
    _db_init()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(100)
    print(f'[relay] HotSpell relay+license listening on port {PORT}')
    while True:
        try:
            c, addr = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
        except Exception as e:
            print(f'[relay] accept error: {e}')


main()
