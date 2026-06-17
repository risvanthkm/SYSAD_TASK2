import socket
import threading
import psycopg2
import psycopg2.pool
import secrets
import bcrypt
import os
import ssl
import json
import hashlib
import subprocess
import yt_dlp, time
import glob
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
from datetime import datetime, timezone
from dotenv import load_dotenv
from helper_functions import *

load_dotenv()

HOST = os.getenv("SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("SERVER_PORT", 8080))
CHUNK_SIZE = 128
MUSIC_PATH = ".\\music"

context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
context.load_cert_chain(os.getenv("TLS_CERT", "server.crt"), os.getenv("TLS_KEY",  "server.key"),)

raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
raw_sock.bind((HOST, PORT))
raw_sock.listen()

db_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=2,
    maxconn=20,
    database=os.getenv("DB_NAME"),
    host=os.getenv("DB_HOST"),
    user=os.getenv("DB_USER"),
    port=os.getenv("DB_PORT"),
    password=os.getenv("DB_PASSWORD"),
)

active_sessions = {}
session_lock = threading.Lock()

password_bf_ban_threshold = 10

class UserState:
    def __init__(self, session_id, user_id):
        self.session_id = session_id
        self.user_id    = user_id

        self.current_track = None
        self.current_chunk = 0

        self.socket    = None
        self.connected = True

        self.stream_thread = None
        self.rtt_ms = 0
        self.buffer_health = 0

        self.pause_event = threading.Event()
        self.stop_event  = threading.Event()

        self.pause_event.set()  


def login_action(auth, IP, cur, user_id, secure_sock, db_conn):
    username = auth["username"]
    password = auth["password"]

    cur.execute("SELECT password FROM users WHERE username = %s", (username,))
    row = cur.fetchone()

    cur.execute("SELECT failed_attempts FROM login_attempts WHERE ip_address = %s", (IP,))
    attps = cur.fetchone()
    failed = attps[0] if attps else 0

    if failed >= password_bf_ban_threshold:
        if not check_banned(IP, cur):
            ban_ip(IP, "brute_force", 1, cur, db_conn)
        return {"status": "error", "message": "IP banned due to too many failed attempts"}

    if row and bcrypt.checkpw(password.encode(), row[0].encode()):
        cur.execute(
            "DELETE FROM active_bans WHERE ip = %s AND reason = %s",
            (IP, "brute_force"),
        )
        cur.execute("DELETE FROM login_attempts WHERE ip_address = %s", (IP,))
        db_conn.commit()

        tok = create_token(username, 3)
        session_id = secrets.token_hex(32)

        state = UserState(session_id, user_id)
        state.socket = secure_sock

        with session_lock:
            active_sessions[session_id] = state

        return {"token": tok, "session_id": session_id}

    else:
        if attps is None:
            cur.execute(
                """
                INSERT INTO login_attempts (username, ip_address, failed_attempts, last_attempt)
                VALUES (%s, %s, 1, CURRENT_TIMESTAMP)
                """,
                (username, IP),
            )
        else:
            cur.execute(
                """
                UPDATE login_attempts
                SET failed_attempts = failed_attempts + 1,
                    last_attempt    = CURRENT_TIMESTAMP
                WHERE ip_address = %s
                """,
                (IP,),
            )
        db_conn.commit()

        new_failed = failed + 1
        if new_failed >= password_bf_ban_threshold:
            ban_ip(IP, "brute_force", 1, cur, db_conn)
            return {"status": "error", "message": "IP banned due to too many failed attempts"}

        return {"status": "error", "message": "invalid credentials"}


def connect(raw_conn, addr):
    try:
        conn = context.wrap_socket(raw_conn, server_side=True)
    except ssl.SSLError as e:
        print(f"TLS handshake failed from {addr}: {e}")
        raw_conn.close()
        return

    db_conn = db_pool.getconn()
    cur = db_conn.cursor()
    state = None

    try:
        if check_banned(addr[0], cur):
            send_json(conn, {"status": "error", "message": "IP is banned"})
            conn.close()
            return

        print(f"[{datetime.now()}] [Connected]", addr)

        # ── FIX: use framed recv so large auth messages aren't truncated
        auth = recv_json(conn)
        if auth is None:
            conn.close()
            return

        if auth["type"] == "LOGIN":
            cur.execute("SELECT id FROM users WHERE username = %s", (auth["username"],))
            row = cur.fetchone()
            user_id = row[0] if row else None

            result = login_action(auth, addr[0], cur, user_id, conn, db_conn)

            if result.get("status") == "error":
                send_json(conn, result)
                conn.close()
                return

            send_json(conn, result)
            send_data(conn, user_id, cur)

            with session_lock:
                state = active_sessions.get(result["session_id"])

        elif auth["type"] == "RECONNECT":

            if not verify_token(auth.get("token", "")):
                send_json(conn, {"status": "error", "message": "invalid token"})
                conn.close()
                return

            with session_lock:
                state = active_sessions.get(auth.get("session_id"))

            if state is None:
                send_json(conn, {"status": "error", "message": "session not found"})
                conn.close()
                return

            state.socket = conn
            state.connected = True
            user_id = state.user_id

        else:
            send_json(conn, {"status": "error", "message": "unknown auth type"})
            conn.close()
            return

        while True:
            req = recv_json(conn)

            if req is None:
                if state:
                    state.connected = False
                break

            process_requests(req, db_conn, user_id, conn, cur, state)

    except Exception as e:
        print(f"Error in connect() for {addr}: {e}")

    finally:
        try:
            conn.close()
        except Exception:
            pass
        db_pool.putconn(db_conn)

os.makedirs("cache", exist_ok=True)
ffmpeg_sem = threading.Semaphore(3) 

def get_transcoded(song_path, bitrate):
    key = hashlib.md5(f"{song_path}{bitrate}".encode()).hexdigest()
    out = f"cache/{key}.mp3"
    if not os.path.exists(out):
        with ffmpeg_sem:             
            subprocess.run([
                "ffmpeg", "-y", "-i", song_path,
                "-b:a", bitrate, "-vn", out
            ], capture_output=True)
    return out


def get_bitrate(rtt_ms):
    if rtt_ms < 80:
        return None       
    elif rtt_ms < 200:
        return "128k"
    else:
        return "64k"

def send_song(conn, db_conn, user_id, song_id, song_path, cur, state):
    if state.stream_thread and state.stream_thread.is_alive():
        state.stop_event.set()
        state.pause_event.set()    
        state.stream_thread.join()

    state.stop_event.clear()
    state.pause_event.set()
    state.current_chunk = 0

    timenow = datetime.now(timezone.utc)
    cur.execute(
        "INSERT INTO history (user_id, song_id, played_at) VALUES (%s, %s, %s)",
        (user_id, song_id, timenow),
    )
    db_conn.commit()   

    print("Song requested", song_path)
    state.current_track = song_id
    state.stream_thread = threading.Thread(
        target=stream_song, args=(state, song_path), daemon=True
    )
    state.stream_thread.start()


def stream_song(state, song_path):
    try:
        with open(song_path, "rb") as f:
            f.seek(state.current_chunk * CHUNK_SIZE)
            while not state.stop_event.is_set():
                state.pause_event.wait()
                if state.stop_event.is_set():
                    break
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    print("Song completely sent...")
                    break
                try:
                    header = MSG_TYPE_AUDIO + len(chunk).to_bytes(4, "big")
                    state.socket.sendall(header + chunk)
                    state.current_chunk += 1
                    print(f"Sent chunk {state.current_chunk}")
                except Exception as e:
                    print("Stream send error:", e)
                    break
    except FileNotFoundError:
        print(f"Music file not found: {song_path}")
    finally:
        state.current_chunk = 0

def process_requests(req, db_conn, user_id, conn, cur, state):
    if not verify_token(req.get("token", "")):
        send_json(conn, {"status": "error", "message": "invalid or expired token"})
        return

    command = req.get("command")

    if command == "PLAY":
        song_id = req.get("song_id")

        cur.execute("SELECT * FROM tracks WHERE song_id = %s", (song_id,))
        song_row = cur.fetchone()
        if song_row is None:
            send_json(conn, {"status": "error", "message": "track not found"})
            return
        song_path      = song_row[-2]

        bitrate = get_bitrate(state.rtt_ms)
        if bitrate:
            song_path = get_transcoded(song_path, bitrate)
        send_song(conn, db_conn, user_id, song_id, song_path, cur, state)

    elif command == "PAUSE":
        state.pause_event.clear()
        send_json(conn, {"status": "paused"})

    elif command == "RESUME":
        state.pause_event.set()
        send_json(conn, {"status": "playing"})

    elif command == "STOP":
        state.stop_event.set()
        state.pause_event.set()   
        if state.stream_thread:
            state.stream_thread.join()
        state.stop_event.clear()
        state.current_chunk = 0
        send_json(conn, {"status": "stopped"})

    elif command == "PING":
        send_json(conn, {"command": "PONG", "ts": req["ts"], "token": req["token"]})

    elif command == "DOWNLOAD":
        urls = req.get("urls", [])

        threading.Thread(
            target=download_songs,
            args=(urls, db_conn),
            daemon=True
        ).start()
        send_json(conn, {"status": "download started"})

    elif command =="BUFFER_STATUS":
        state.buffer_health = req.get('health', 0)

    elif command == "GET_PLAYLIST":
        playlist_id = req.get("playlist_id")
        cur.execute(
            "SELECT playlist_id FROM playlists WHERE playlist_id = %s AND user_id = %s",
            (playlist_id, user_id)
        )
        if cur.fetchone() is None:
            send_json(conn, {"status": "error", "message": "playlist not found"})
            return

        cur.execute(
            """
            SELECT t.song_id, t.title, t.artist, t.genre
            FROM playlist_songs ps
            JOIN tracks t ON ps.song_id = t.song_id
            WHERE ps.playlist_id = %s
            """,
            (playlist_id,)
        )
        track_list = [
            {"song_id": r[0], "title": r[1], "artist": r[2], "genre": r[3]}
            for r in cur.fetchall()
        ]
        send_json(conn, {"status": "ok", "tracks": track_list})

    elif command == "ADD_TO_PLAYLIST":
        playlist_id = req.get("playlist_id")
        song_id     = req.get("song_id")
        cur.execute(
            "SELECT playlist_id FROM playlists WHERE playlist_id = %s AND user_id = %s",
            (playlist_id, user_id)
        )
        if cur.fetchone() is None:
            send_json(conn, {"status": "error", "message": "playlist not found"})
            return
        try:
            cur.execute(
                "INSERT INTO playlist_songs(playlist_id, song_id) VALUES (%s, %s)",
                (playlist_id, song_id)
            )
            db_conn.commit()
            send_json(conn, {"status": "ok"})
        except Exception as e:
            db_conn.rollback()
            send_json(conn, {"status": "error", "message": "could not add song"})

    elif command == "DELETE_PLAYLIST":
        playlist_id = req.get("playlist_id")
        cur.execute(
            "SELECT playlist_id FROM playlists WHERE playlist_id = %s AND user_id = %s",
            (playlist_id, user_id)
        )
        if cur.fetchone() is None:
            send_json(conn, {"status": "error", "message": "playlist not found"})
            return
        try:
            cur.execute("BEGIN")
            cur.execute("DELETE FROM playlist_songs WHERE playlist_id = %s", (playlist_id,))
            cur.execute("DELETE FROM playlists WHERE playlist_id = %s", (playlist_id,))
            cur.execute("COMMIT")
            send_json(conn, {"status": "ok"})
        except Exception as e:
            cur.execute("ROLLBACK")
            send_json(conn, {"status": "error", "message": "delete failed"})

    elif command == "CREATE_PLAYLIST":
        try:
            cur.execute(
                """
                INSERT INTO playlists(user_id, playlist_name)
                VALUES (%s, %s) RETURNING playlist_id
                """,
                (user_id, req["name"])
            )
            playlist_id = cur.fetchone()[0]

            for song in req.get("song_id", []):
                cur.execute(
                    "INSERT INTO playlist_songs(playlist_id, song_id) VALUES (%s, %s)",
                    (playlist_id, song)
                )

            db_conn.commit() 
            send_json(conn, {"status": "ok", "playlist_id": playlist_id})

        except Exception as e:
            db_conn.rollback() 
            send_json(conn, {"status": "error", "message": "playlist creation failed"})
            print(f"CREATE_PLAYLIST failed: {e}")

    else:
        send_json(conn, {"status": "error", "message": f"unknown command: {command}"})


def download_songs(urls, db_conn):
    for url in urls:
        subprocess.run([
            "yt-dlp",
            "-x",                        
            "--audio-format", "mp3",
            "--restrict-filenames",      
            "-o", f"{MUSIC_PATH}/%(title)s.%(ext)s",
            url
        ])
        time.sleep(2)                   

    scan_and_update(db_conn)
    broadcast({"type": "LIBRARY_UPDATED"})

def scan_and_update(db_conn):
    cur = db_conn.cursor()

    files = set(os.path.abspath(p)
                for p in glob.glob(f"{MUSIC_PATH}/**/*.mp3", recursive=True))

    cur.execute("SELECT song_id, file_path FROM tracks")
    in_db = {row[1]: row[0] for row in cur.fetchall()}

    for path in files - in_db.keys():
        try:
            audio  = MP3(path, ID3=EasyID3)
            if audio is not None:
                title  = audio.get("title",  ["Unknown"])[0] # type: ignore
                artist = audio.get("artist", ["Unknown"])[0] # type: ignore
            cur.execute(
                "INSERT INTO tracks(title, artist, file_path) VALUES (%s,%s,%s)",
                (title, artist, path)
            )
        except Exception as e:
            print(f"Could not parse {path}: {e}")

    for path in in_db.keys() - files:
        song_id = in_db[path]
        cur.execute("DELETE FROM playlist_songs WHERE song_id = %s", (song_id,))
        cur.execute("DELETE FROM history WHERE song_id = %s", (song_id,))
        cur.execute("DELETE FROM tracks WHERE file_path = %s", (path,))

    db_conn.commit()
    cur.close()

def scanner_thread(db_conn):
    while True:
        time.sleep(60)
        scan_and_update(db_conn)
        broadcast({"type": "LIBRARY_UPDATED"}) 

def broadcast(msg):
    with session_lock:
        for state in active_sessions.values():
            try:
                send_json(state.socket, msg)
            except Exception:
                pass

def main():
    backup_thread = threading.Thread(target=backup_thread_fn, daemon=True)
    backup_thread.start()

    scanner_conn = db_pool.getconn()
    threading.Thread(target=scanner_thread, args=(scanner_conn,), daemon=True).start()

    print(f"Server listening on {HOST}:{PORT}")
    while True:
        conn, addr = raw_sock.accept()
        t = threading.Thread(target=connect, args=(conn, addr), daemon=True)
        t.start()


if __name__ == "__main__":
    main()