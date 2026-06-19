import socket
import ssl
import json
import threading
import time
import os
import sys
import pathlib
import tempfile
import vlc
from banner import *

HOST = "localhost"
PORT = 8080

CHUNK_SIZE = 128
BUFFER_THRESHOLD = 128 * 1024
SESSION_FILE = pathlib.Path(".session")

MSG_TYPE_JSON = b'\x01'
MSG_TYPE_AUDIO = b'\x02'

conn = None
token = None
session_id = None
songs = []      
all_songs = []      
history = []
playlists = []       
now_playing  = None
rtt_ms = 0
buffer_health = 0

audio_player  = None
audio_file = None
audio_started = False
audio_bytes = 0

print_lock = threading.Lock()
running = True


def out(msg):
    with print_lock:
        print(msg)


def send_json(sock, obj):
    data = json.dumps(obj).encode()
    header = MSG_TYPE_JSON + len(data).to_bytes(4, "big")
    sock.sendall(header + data)


def recv_msg(sock):
    header = b""
    while len(header) < 5:
        chunk = sock.recv(5 - len(header))
        if not chunk:
            return None, None
        header += chunk
    msg_type = header[:1]
    length   = int.from_bytes(header[1:], "big")
    body     = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            return None, None
        body += chunk
    if msg_type == MSG_TYPE_JSON:
        return "json", json.loads(body.decode())
    return "audio", body


def save_session(tok, sid):
    SESSION_FILE.write_text(json.dumps({"token": tok, "session_id": sid}))


def load_session():
    try:
        return json.loads(SESSION_FILE.read_text())
    except Exception:
        return None


def clear_session():
    SESSION_FILE.unlink(missing_ok=True)


def make_tls_conn():
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    c = ctx.wrap_socket(raw, server_hostname=HOST)
    c.connect((HOST, PORT))
    return c


def reconnect():
    global conn, token, session_id

    saved = load_session()
    if not saved:
        return False
    tok = saved["token"]
    sid = saved["session_id"]

    for attempt in range(1, 6):
        delay = 2 ** (attempt - 1)
        out(f"[reconnect] attempt {attempt}/5 in {delay}s...")
        time.sleep(delay)
        try:
            new_conn = make_tls_conn()
            send_json(new_conn, {"type": "RECONNECT", "token": tok, "session_id": sid})
            mt, resp = recv_msg(new_conn)
            if mt == "json" and resp.get("status") == "ok":
                conn = new_conn
                token = tok
                session_id = sid
                out(f"[reconnect] success, resuming at chunk {resp.get('resumed_at_chunk', 0)}")
                return True
            new_conn.close()
        except Exception as e:
            out(f"[reconnect] attempt {attempt} failed: {e}")

    clear_session()
    return False

def login():
    global conn, token, session_id, songs, all_songs, history, playlists, username
    print("            Welcome to DeltaPlay. Please Login.")

    username = input("username: ").strip()
    password = input("password: ")

    try:
        new_conn = make_tls_conn()
    except Exception as e:
        print(f"Couldn't reach {HOST}:{PORT} >> {e}")
        return False

    send_json(new_conn, {"type": "LOGIN", "username": username, "password": password})

    resp = None
    while resp is None:
        mt, data = recv_msg(new_conn)
        if data is None:
            print("Server closed connection during login")
            return False
        if mt == "json" and ("token" in data or data.get("status") == "error"):
            resp = data

    if resp.get("status") == "error":
        print(f"login failed: {resp.get('message')}")
        return False

    token = resp["token"]
    session_id = resp["session_id"]
    conn = new_conn
    save_session(token, session_id)

    lib = None
    while lib is None:
        mt, data = recv_msg(conn)
        if data is None:
            print("Server closed before sending library")
            return False
        if mt == "json" and "songs" in data:
            lib = data

    songs = lib.get("songs", [])
    all_songs = list(songs)
    history = lib.get("history", [])
    playlists = lib.get("saved_playlists", [])

    print(f"Login OK. {len(songs)} songs in library, {len(playlists)} saved playlists.\n")
    return True


def listen_loop():
    global conn, audio_file, audio_started, audio_bytes, audio_player, songs, playlists

    while running:
        try:
            mt, data = recv_msg(conn)

            if data is None:
                out("!!!! Disconnected from server - reconnecting... !!!!")
                if not reconnect():
                    out("Could not reconnect. Type 'quit' and restart.")
                    break
                continue

            if mt == "json":
                handle_server_msg(data)

            elif mt == "audio":
                if audio_file is None:
                    continue
                audio_file.write(data)
                audio_file.flush()
                audio_bytes += len(data)
                if not audio_started and audio_bytes >= BUFFER_THRESHOLD:
                    audio_player  = vlc.MediaPlayer(audio_file.name)
                    audio_player.play()
                    audio_started = True
                    out("Buffered enough audio, Playing the Music")

        except Exception as e:
            out(f"Error: {e} - reconnecting...")
            if not reconnect():
                out("Could not reconnect.")
                break

def handle_server_msg(msg):
    global songs, playlists
    cmd = msg.get("command") or msg.get("status") or msg.get("type")

    if cmd == "PONG":
        rtt_ms = (time.monotonic() - t0) * 1000

    elif cmd == "LIBRARY_UPDATED":
        out("Server library changed - run 'library' to refresh")

    elif cmd == "paused":
        out("Paused")

    elif cmd == "playing":
        out("Resumed")

    elif cmd == "stopped":
        out("Stopped")

    elif cmd == "PLAYLIST_CREATED":
        playlists.append({"playlist_id": msg["playlist_id"], "playlist_name": msg["name"]})
        out(f"Created '{msg['name']}' (id={msg['playlist_id']})")

    elif cmd == "PLAYLIST_DELETED":
        pid = msg["playlist_id"]
        playlists[:] = [p for p in playlists if p["playlist_id"] != pid]
        out(f"Deleted id={pid}")

    elif cmd == "PLAYLIST_TRACKS":
        songs = msg.get("tracks", [])
        out(f"Loaded {len(songs)} tracks - run 'list' to view them")

    elif cmd == "SONG_ADDED":
        out("Song added")

    elif cmd == "download started":
        out("Queued on server")

    elif cmd == "error":
        out(f"[error] {msg.get('message')}")

    else:
        out(f"[server] {msg}")


def heartbeat_loop():
    global rtt_ms, buffer_health, t0
    while running:
        time.sleep(5)
        if not conn or not token:
            continue
        try:
            t0 = time.monotonic()
            send_json(conn, {"command": "PING", "token": token, "ts": t0})
            buffer_health = min(100, int((audio_bytes / BUFFER_THRESHOLD) * 100))
        except Exception:
            pass


def buffer_report_loop():
    while running:
        time.sleep(1)
        if not token or not conn:
            continue
        health = min(100, int((audio_bytes / BUFFER_THRESHOLD) * 100))
        try:
            send_json(conn, {"command": "BUFFER_STATUS", "token": token, "health": health})
        except Exception:
            pass


def cmd_list():
    if not songs:
        print("[no songs to show]")
        return
    print(f" ID TITLE ARTIST GENRE ")
    print("-" * 79)
    for i, s in enumerate(songs):
        print(f"{i}{s.get('title','?')[:33]}{s.get('artist','?')[:23]}{s.get('genre')}")
    print()


def cmd_library():
    global songs
    songs = list(all_songs)
    print(f"showing full library: {len(songs)} songs")
    cmd_list()


def cmd_play(idx_str):
    global now_playing, audio_file, audio_started, audio_bytes, audio_player

    if not idx_str:
        print("usage: play <index>   (run 'list' to see indices)")
        return
    try:
        idx = int(idx_str)
    except ValueError:
        print("index must be a number")
        return
    if idx < 0 or idx >= len(songs):
        print("index out of range")
        return

    audio_started = False
    audio_bytes   = 0
    if audio_player:
        audio_player.stop()
    if audio_file:
        try:
            name = audio_file.name
            audio_file.close()
            os.unlink(name)
        except Exception:
            pass
    audio_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)

    song        = songs[idx]
    now_playing = song
    send_json(conn, {"command": "PLAY", "song_id": song["song_id"], "token": token})
    print(f"Requested to Play: {song['title']}")


def cmd_pause():
    if audio_player:
        audio_player.pause()
    send_json(conn, {"command": "PAUSE", "token": token})


def cmd_resume():
    if audio_player:
        audio_player.pause()  
    send_json(conn, {"command": "RESUME", "token": token})


def cmd_stop():
    global now_playing
    if audio_player:
        audio_player.stop()
    send_json(conn, {"command": "STOP", "token": token})
    now_playing = None


def cmd_status():
    np_title = f"{now_playing['title']} - {now_playing['artist']}" if now_playing else "nothing"
    print(f"Now playing : {np_title}")
    print(f"rtt : {rtt_ms:.0f} ms")
    print(f"buffer: {buffer_health}%")


def cmd_playlists():
    if not playlists:
        print("(no playlists yet)")
        return
    print('#       NAME')
    print("-" * 40)
    for i, p in enumerate(playlists):
        print(f"{i}    {p['playlist_name']}")
    print()


def cmd_create_playlist(rest):
    rest = rest.strip()
    if not rest:
        print("usage: create_playlist <name> [song_index1,song_index2,...]")
        return

    parts = rest.rsplit(" ", 1)
    song_ids = []
    name = rest

    if len(parts) == 2 and all(p.strip().isdigit() for p in parts[1].split(",") if p.strip()):
        name = parts[0]
        idx_list = [int(p) for p in parts[1].split(",") if p.strip()]
        song_ids = [songs[i]["song_id"] for i in idx_list if 0 <= i < len(songs)]

    send_json(conn, {
        "command":  "CREATE_PLAYLIST",
        "token": token,
        "name": name,
        "song_id": song_ids,
    })
    print(f"Creating playlist '{name}' with {len(song_ids)} song(s)...")


def cmd_load_playlist(idx_str):
    if not idx_str:
        print("usage: load_playlist <index> (run 'playlists')")
        return
    
    idx = int(idx_str)

    if idx < 0 or idx >= len(playlists):
        print("index out of range")
        return
    
    pl = playlists[idx]
    send_json(conn, {
        "command":"GET_PLAYLIST",
        "token":token,
        "playlist_id":pl["playlist_id"],
    })
    print(f"loading '{pl['playlist_name']}'...")


def cmd_delete_playlist(idx_str):
    if not idx_str:
        print("usage: delete_playlist <index> (run 'playlists')")
        return
    try:
        idx = int(idx_str)
    except ValueError:
        print("index must be a number")
        return
    if idx < 0 or idx >= len(playlists):
        print("index out of range")
        return
    pl = playlists[idx]
    send_json(conn, {
        "command":     "DELETE_PLAYLIST",
        "token":       token,
        "playlist_id": pl["playlist_id"],
    })
    print(f"deleting '{pl['playlist_name']}'...")


def cmd_add_to_playlist(rest):
    parts = rest.split()
    if len(parts) != 2:
        print("usage: add_to_playlist <playlist_index> <song_index>")
        return
    try:
        pl_idx, song_idx = int(parts[0]), int(parts[1])
    except ValueError:
        print("both arguments must be numbers")
        return
    if pl_idx < 0 or pl_idx >= len(playlists):
        print("playlist index out of range")
        return
    if song_idx < 0 or song_idx >= len(songs):
        print("song index out of range")
        return
    pl   = playlists[pl_idx]
    song = songs[song_idx]
    send_json(conn, {
        "command":     "ADD_TO_PLAYLIST",
        "token":       token,
        "playlist_id": pl["playlist_id"],
        "song_id":     song["song_id"],
    })
    print(f"adding '{song['title']}' to '{pl['playlist_name']}'...")


def cmd_download(path):
    path = path.strip()
    if not path:
        print("usage: download <path to txt file of youtube links>")
        return
    if not os.path.exists(path):
        print(f"file not found: {path}")
        return
    with open(path) as f:
        urls = [l.strip() for l in f if l.strip()]
    if not urls:
        print("no URLs found in file")
        return
    send_json(conn, {"command": "DOWNLOAD", "token": token, "urls": urls})
    print(f"Sent {len(urls)} URL(s) to server for downloading")


def cmd_history():
    if not history:
        print("(no listening history)")
        return
    print('SONG ID       PLAYED AT')
    print("-" * 35)
    for h in history:
        print(f"{h.get('song_id'):<10}---{h.get('played_at') or '-':<25}")
    print()

def command_loop():
    global running
    print(HELP_TEXT)
    while running:
        try:
            line = input(f"{username}@deltaplay: $ ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "quit"

        if not line:
            continue

        if " " in line:
            cmd, rest = line.split(" ", 1)
        else:
            cmd, rest = line, ""
        cmd = cmd.lower()

        if cmd == "quit" or cmd == "exit":
            running = False
            break
        elif cmd == "help":
            print(HELP_TEXT)
        elif cmd == "list":
            cmd_list()
        elif cmd == "library":
            cmd_library()
        elif cmd == "play":
            cmd_play(rest)
        elif cmd == "pause":
            cmd_pause()
        elif cmd == "resume":
            cmd_resume()
        elif cmd == "stop":
            cmd_stop()
        elif cmd == "status":
            cmd_status()
        elif cmd == "playlists":
            cmd_playlists()
        elif cmd == "create_playlist":
            cmd_create_playlist(rest)
        elif cmd == "load_playlist":
            cmd_load_playlist(rest)
        elif cmd == "delete_playlist":
            cmd_delete_playlist(rest)
        elif cmd == "add_to_playlist":
            cmd_add_to_playlist(rest)
        elif cmd == "download":
            cmd_download(rest)
        elif cmd == "history":
            cmd_history()
        else:
            print(f"unknown command: '{cmd}' - type 'help' for a list")

def shutdown():
    global running
    running = False
    if audio_player:
        audio_player.stop()
    if audio_file:
        try:
            name = audio_file.name
            audio_file.close()
            os.unlink(name)
        except Exception:
            pass
    if conn:
        try:
            conn.close()
        except Exception:
            pass

def main():
    if not login():
        return

    threading.Thread(target=listen_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=buffer_report_loop, daemon=True).start()

    try:
        command_loop()
    finally:
        shutdown()
        print("Disconnected from the server .")


if __name__ == "__main__":
    main()