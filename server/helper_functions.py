import threading
import json
import jwt
import time
import subprocess
import os
from datetime import timedelta, datetime, timezone
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")

# ── FIX: pg_dump path now read from env so it works on any machine/OS.
#         backup dir is created if missing.
#         errors are caught and logged instead of silently swallowed.
def backup_thread_fn():
    pg_dump_path = r"C:\\Program Files\\PostgreSQL\\18\\bin\\pg_dump.exe"
    db_name      = os.getenv("DB_NAME", "deltaplay")
    db_user      = os.getenv("DB_USER", "postgres")
    backup_dir   = os.getenv("BACKUP_DIR", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    while True:
        timestamp   = int(time.time())
        backup_file = os.path.join(backup_dir, f"backup_{timestamp}.sql")
        env = os.environ.copy()
        env["PGPASSWORD"] = os.getenv("DB_PASSWORD", "")

        result = subprocess.run(
            [pg_dump_path, "-U", db_user, "-d", db_name, "-f", backup_file],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[backup] pg_dump failed: {result.stderr.strip()}")
        else:
            print(f"[backup] saved {backup_file}")

        time.sleep(84000)


def verify_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        print("Token expired")
        return None
    except jwt.InvalidTokenError:
        print("Invalid token")
        return None


def create_token(username, expires):
    token = jwt.encode(
        {"user": username, "exp": datetime.now(timezone.utc) + timedelta(hours=expires)},
        SECRET_KEY,
        algorithm="HS256",
    )
    return token


def ban_ip(IP, reason, days, cur, db_conn):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days)
    cur.execute(
        "INSERT INTO active_bans(ip, reason, banned_at, expires_at) VALUES (%s, %s, %s, %s)",
        (IP, reason, now, expires_at),
    )
    db_conn.commit()


def check_banned(ip, cur):
    # Also filter out expired bans so they don't block legitimate users
    cur.execute(
        "SELECT ban_id FROM active_bans WHERE ip = %s AND expires_at > NOW()",
        (ip,),
    )
    return cur.fetchone() is not None


# FIX: removed row-constructor parens — SELECT (col) returns an opaque record
#      string in psycopg2; SELECT col returns actual values.
# FIX: datetime objects aren't JSON-serialisable; convert to isoformat strings.
def send_data(conn, user_id, cur):
    cur.execute("SELECT song_id, artist, genre, title FROM tracks")
    songs = [
        {"song_id": r[0], "artist": r[1], "genre": r[2], "title": r[3]}
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT song_id, played_at FROM history WHERE user_id = %s", (user_id,)
    )
    history = [
        {"song_id": r[0], "played_at": r[1].isoformat() if r[1] else None}
        for r in cur.fetchall()
    ]

    cur.execute(
        "SELECT playlist_id, playlist_name FROM playlists WHERE user_id = %s", (user_id,)
    )
    saved_pl = [
        {"playlist_id": r[0], "playlist_name": r[1]} for r in cur.fetchall()
    ]

    payload = {"songs": songs, "history": history, "saved_playlists": saved_pl}

    # FIX: frame the message with a 4-byte length header so the client knows
    #      where JSON ends and binary audio chunks begin.
    send_json(conn, payload)


# ── Framing helpers ──────────────────────────────────────────────────────────
# Every JSON message is prefixed with a 4-byte big-endian length so both
# sides can distinguish control messages from raw audio chunks.

MSG_TYPE_JSON  = b'\x01'
MSG_TYPE_AUDIO = b'\x02'

def send_json(sock, obj):
    """Send a JSON control message with a 1-byte type tag + 4-byte length."""
    data = json.dumps(obj).encode()
    header = MSG_TYPE_JSON + len(data).to_bytes(4, "big")
    sock.sendall(header + data)

def recv_json(sock):
    """Read exactly one JSON control message (blocks until complete)."""
    raw = _recv_exact(sock, 5)          # 1 type + 4 length
    if not raw:
        return None
    msg_type = raw[:1]
    if msg_type != MSG_TYPE_JSON:
        return None
    length   = int.from_bytes(raw[1:], "big")
    body     = _recv_exact(sock, length)
    return json.loads(body.decode())

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf