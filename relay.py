"""
HotSpell Relay Server
Deploy on Railway (or any host with a public TCP port).
Two players connect with a shared 6-character room code;
the server pairs them and forwards bytes transparently.

Railway requires a web process to respond to HTTP health checks.
This server runs a minimal HTTP health-check listener on PORT (Railway's
assigned HTTP port) and the relay on RELAY_PORT (our TCP proxy target).
"""
import os, socket, threading, secrets, time

# Railway assigns PORT for its HTTP health-check; we use RELAY_PORT for the relay.
HTTP_PORT  = int(os.environ.get('PORT', 0))          # Railway health-check port
RELAY_PORT = int(os.environ.get('RELAY_PORT', 8080)) # TCP proxy target port

_rooms: dict = {}
_lock         = threading.Lock()


# ── Cleanup stale rooms ───────────────────────────────────────────────────────
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


# ── Byte-pipe between two sockets ─────────────────────────────────────────────
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


# ── Read one line from socket ─────────────────────────────────────────────────
def _readline(conn: socket.socket) -> str:
    buf = b''
    while len(buf) < 64:
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


# ── Handle one relay connection ───────────────────────────────────────────────
def _handle(conn: socket.socket):
    conn.settimeout(15)
    try:
        line = _readline(conn)
        if not line or ':' not in line:
            conn.close()
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

        else:
            conn.close()

    except Exception as e:
        print(f'[relay] error: {e}')
        try: conn.close()
        except Exception: pass


# ── HTTP health-check server (Railway web process requirement) ────────────────
def _http_health(port: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(16)
    print(f'[relay] HTTP health-check listening on port {port}')
    while True:
        try:
            c, _ = srv.accept()
            try:
                c.recv(4096)
                c.sendall(
                    b'HTTP/1.1 200 OK\r\n'
                    b'Content-Length: 2\r\n'
                    b'Connection: close\r\n\r\nOK'
                )
            except Exception:
                pass
            finally:
                try: c.close()
                except Exception: pass
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Start HTTP health-check thread if Railway gave us a separate PORT
    if HTTP_PORT and HTTP_PORT != RELAY_PORT:
        threading.Thread(target=_http_health, args=(HTTP_PORT,), daemon=True).start()

    # Start relay TCP server
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', RELAY_PORT))
    srv.listen(100)
    print(f'[relay] HotSpell relay listening on port {RELAY_PORT}')
    while True:
        try:
            c, addr = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
        except Exception as e:
            print(f'[relay] accept error: {e}')


main()

