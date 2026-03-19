"""
HotSpell Relay Server — Railway deployment
Listens on Railway's assigned PORT.
Detects HTTP health checks (no colon in first line) vs relay protocol (HOST:/JOIN:).
"""
import os, socket, threading, secrets, time

PORT = int(os.environ.get('PORT', 8080))

_rooms: dict = {}
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
    while len(buf) < 256:
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

        # HTTP health check: request line has no colon (e.g. "GET / HTTP/1.1")
        # Relay protocol always has colon: "HOST:" or "JOIN:ABCDEF"
        if ':' not in line:
            try:
                conn.recv(4096)  # drain remaining headers
                conn.sendall(
                    b'HTTP/1.1 200 OK\r\n'
                    b'Content-Length: 2\r\n'
                    b'Connection: close\r\n\r\nOK'
                )
            except Exception:
                pass
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


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(100)
    print(f'[relay] HotSpell relay listening on port {PORT}')
    while True:
        try:
            c, addr = srv.accept()
            threading.Thread(target=_handle, args=(c,), daemon=True).start()
        except Exception as e:
            print(f'[relay] accept error: {e}')


main()
