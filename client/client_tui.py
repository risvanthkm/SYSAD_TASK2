import socket
import ssl
import json
import threading
import time
import os

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, ListView, ListItem, Label, Input, Button, Log
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual import events

import logging
logging.basicConfig(
    filename="client_log.txt",
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("deltaplay")

# ── Server connection settings ──────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8080

# ── Framing helpers (must match server) ─────────────────────────────────────
MSG_TYPE_JSON  = b'\x01'
MSG_TYPE_AUDIO = b'\x02'

def send_json(sock, obj):
    log.debug(f"send_json → {obj}")
    data   = json.dumps(obj).encode()
    header = MSG_TYPE_JSON + len(data).to_bytes(4, "big")
    sock.sendall(header + data)

def recv_msg(sock):
    """Returns (msg_type, data). msg_type is 'json' or 'audio'."""
    header = b""
    while len(header) < 5:
        chunk = sock.recv(5 - len(header))
        if not chunk:
            log.warning("recv_msg: connection closed while reading header")
            return None, None
        header += chunk

    msg_type = header[:1]
    length   = int.from_bytes(header[1:], "big")
    log.debug(f"recv_msg: type={'JSON' if msg_type == MSG_TYPE_JSON else 'AUDIO'} length={length}")

    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            log.warning("recv_msg: connection closed while reading body")
            return None, None
        body += chunk

    if msg_type == MSG_TYPE_JSON:
        parsed = json.loads(body.decode())
        log.debug(f"recv_msg ← {parsed}")
        return "json", parsed
    else:
        return "audio", body


# ── Global state ─────────────────────────────────────────────────────────────
conn        = None
token       = None
session_id  = None
songs       = []
history     = []
playlists   = []
rtt_ms      = 0
now_playing = None


# ═══════════════════════════════════════════════════════════════════════════
# LOGIN SCREEN
# ═══════════════════════════════════════════════════════════════════════════
class LoginScreen(Screen):

    CSS = """
    LoginScreen {
        align: center middle;
    }
    #box {
        width: 50;
        height: 18;
        border: round green;
        padding: 1 2;
    }
    #title {
        text-align: center;
        color: green;
        text-style: bold;
        margin-bottom: 1;
    }
    Input {
        margin-bottom: 1;
    }
    #error {
        color: red;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label("🎵  DeltaPlay", id="title")
            yield Input(placeholder="Username", id="username")
            yield Input(placeholder="Password", password=True, id="password")
            yield Button("Login", variant="success", id="login_btn")
            yield Label("", id="error")

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "login_btn":
            self.do_login()

    def on_input_submitted(self, event: Input.Submitted):
        self.do_login()

    def do_login(self):
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value.strip()
        err      = self.query_one("#error", Label)

        if not username or not password:
            err.update("Please fill in both fields.")
            return

        log.info(f"Login attempt for user: {username}")
        err.update("Connecting...")

        threading.Thread(
            target=self._login_thread,
            args=(username, password),
            daemon=True
        ).start()

    def _login_thread(self, username, password):
        global conn, token, session_id, songs, history, playlists

        def show_error(msg):
            self.app.call_from_thread(
                self.query_one("#error", Label).update, msg
            )

        try:
            log.info(f"Connecting to {HOST}:{PORT}")
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            conn = ctx.wrap_socket(raw, server_hostname=HOST)
            conn.connect((HOST, PORT))
            log.info("TLS connection established")

            send_json(conn, {
                "type":     "LOGIN",
                "username": username,
                "password": password,
            })
            log.info("Login request sent, waiting for response...")

            _, resp = recv_msg(conn)
            if resp is None:
                log.error("No response from server after login")
                show_error("No response from server")
                conn.close()
                conn = None
                return

            if resp.get("status") == "error":
                log.warning(f"Login rejected: {resp.get('message')}")
                show_error(resp.get("message", "Login failed"))
                conn.close()
                conn = None
                return

            token      = resp["token"]
            session_id = resp["session_id"]
            log.info(f"Login successful. Session: {session_id[:8]}...")

            log.info("Waiting for library data...")
            _, data = recv_msg(conn)
            if data is None:
                log.error("No library data received")
                show_error("Failed to receive library data")
                return

            songs     = data.get("songs", [])
            history   = data.get("history", [])
            playlists = data.get("saved_playlists", [])
            log.info(f"Library loaded: {len(songs)} songs, {len(playlists)} playlists, {len(history)} history entries")

            self.app.call_from_thread(self.app.push_screen, MainScreen())

        except ConnectionRefusedError:
            log.error(f"Connection refused to {HOST}:{PORT}")
            show_error(f"Could not connect to server at {HOST}:{PORT}")
        except ssl.SSLError as e:
            log.exception("SSL error during login")
            show_error(f"SSL error: {e}")
        except Exception as e:
            log.exception("Unexpected error during login")
            show_error(f"Error: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SCREEN
# ═══════════════════════════════════════════════════════════════════════════
class MainScreen(Screen):

    CSS = """
    #sidebar {
        width: 30;
        border-right: solid green;
    }
    #main {
        width: 1fr;
    }
    #now_playing {
        height: 5;
        border: round yellow;
        padding: 0 1;
        margin: 1;
        color: yellow;
    }
    #controls {
        height: 3;
        margin: 0 1;
    }
    #controls Button {
        margin-right: 1;
    }
    #song_list {
        height: 1fr;
        border: round green;
        margin: 1;
    }
    #log_box {
        height: 8;
        border: round gray;
        margin: 1;
    }
    #rtt {
        margin: 1;
        color: gray;
    }
    #sidebar_title {
        text-align: center;
        color: green;
        text-style: bold;
        padding: 1;
    }
    #download_input {
        margin: 1;
    }
    #download_btn {
        margin: 0 1 1 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Playlists", id="sidebar_title")
                yield ListView(id="playlist_list")
                yield Label("── Download ──")
                yield Input(placeholder="links.txt path", id="download_input")
                yield Button("⬇ Download", id="download_btn", variant="primary")
                yield Label("── New Playlist ──")
                yield Input(placeholder="Playlist name", id="playlist_name_input")
                yield Button("Create", id="create_playlist_btn", variant="success")
                yield Button("Load", id="load_playlist_btn", variant="primary")

            with Vertical(id="main"):
                yield Static("Nothing playing", id="now_playing")

                with Horizontal(id="controls"):
                    yield Button("▶ Play",   id="play_btn",   variant="success")
                    yield Button("⏸ Pause",  id="pause_btn")
                    yield Button("▶ Resume", id="resume_btn", variant="primary")
                    yield Button("⏹ Stop",   id="stop_btn",   variant="error")

                yield ListView(id="song_list")
                yield Log(id="log_box", max_lines=50)
                yield Label("RTT: -- ms", id="rtt")

        yield Footer()

    def on_mount(self):
        log.info("MainScreen mounted")

        song_lv = self.query_one("#song_list", ListView)
        for s in songs:
            song_lv.append(ListItem(Label(f"🎵  {s['title']}  —  {s['artist']}")))
        log.info(f"Song list populated with {len(songs)} items")

        pl_lv = self.query_one("#playlist_list", ListView)
        for pl in playlists:
            pl_lv.append(ListItem(Label(f"📋  {pl['playlist_name']}")))
        log.info(f"Playlist sidebar populated with {len(playlists)} items")

        threading.Thread(target=self.listen_to_server, daemon=True).start()
        log.info("Server listener thread started")

        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        log.info("Heartbeat thread started")

    # ── Server listener ──────────────────────────────────────────────────
    def listen_to_server(self):
        log.info("listen_to_server: started")
        while True:
            try:
                msg_type, data = recv_msg(conn)

                if data is None:
                    log.warning("listen_to_server: received None — server disconnected")
                    self.log_msg("Disconnected from server.")
                    break

                if msg_type == "json":
                    log.debug(f"listen_to_server: JSON message received: {data}")
                    self.handle_server_msg(data)
                elif msg_type == "audio":
                    log.debug("listen_to_server: audio chunk received (not played yet)")

            except Exception as e:
                log.exception("listen_to_server: crashed")
                self.log_msg(f"Listener error: {e}")
                break

        log.info("listen_to_server: exited loop")

    def do_create_playlist(self):
        name = self.query_one("#playlist_name_input", Input).value.strip()
        if not name:
            self.log_msg("Enter a playlist name.")
            return
        # Collect currently selected songs (optional — can start empty)
        send_json(conn, {
            "command": "CREATE_PLAYLIST",
            "token":   token,
            "name":    name,
            "song_id": [],   # fill with selected IDs if you want
        })
        self.log_msg(f"Creating playlist: {name}")

    def do_load_playlist(self):
        pl_lv = self.query_one("#playlist_list", ListView)
        idx = pl_lv.index
        if idx is None or idx >= len(playlists):
            self.log_msg("Select a playlist first.")
            return
        playlist_id = playlists[idx]["playlist_id"]
        send_json(conn, {
            "command":     "GET_PLAYLIST",
            "token":       token,
            "playlist_id": playlist_id,
        })
        self.log_msg(f"Loading playlist {playlist_id}...")

    def _refresh_song_list(self):
            lv = self.query_one("#song_list", ListView)
            lv.clear()
            for s in songs:
                lv.append(ListItem(Label(f"🎵  {s['title']}  —  {s['artist']}")))
            self.log_msg(f"Loaded {len(songs)} tracks.")

    def handle_server_msg(self, msg):
        t = msg.get("type") or msg.get("status") or msg.get("command")
        log.debug(f"handle_server_msg: type='{t}' full={msg}")

        if t == "PONG":
            pass   # RTT handled in heartbeat_loop
        elif t == "LIBRARY_UPDATED":
            log.info("Library updated by server")
            self.log_msg("📚 Library updated — restart to refresh.")
        elif msg.get("status") == "ok" and "tracks" in msg:
            # Playlist loaded — replace song list view with these tracks
            global songs
            songs = msg["tracks"]
            self.app.call_from_thread(self._refresh_song_list)
        
        elif t == "paused":
            log.info("Playback paused")
            self.log_msg("⏸  Paused")
        elif t == "playing":
            log.info("Playback resumed")
            self.log_msg("▶  Playing")
        elif t == "stopped":
            log.info("Playback stopped")
            self.log_msg("⏹  Stopped")
            self.app.call_from_thread(
                self.query_one("#now_playing", Static).update,
                "Nothing playing"
            )
        elif t == "error":
            log.error(f"Server error message: {msg.get('message')}")
            self.log_msg(f"❌  {msg.get('message')}")
        elif t == "download started":
            log.info("Server started download")
            self.log_msg("⬇  Download started in background...")
        elif t == "ok":
            log.info(f"Server OK response: {msg}")
            self.log_msg("✅  Done")
        else:
            log.warning(f"handle_server_msg: unknown message type '{t}': {msg}")

    # ── Heartbeat ────────────────────────────────────────────────────────
    def heartbeat_loop(self):
        global rtt_ms
        log.info("heartbeat_loop: started")
        while True:
            time.sleep(5)
            try:
                t0 = time.monotonic()
                send_json(conn, {"command": "PING", "token": token, "ts": t0})
                rtt_ms = (time.monotonic() - t0) * 1000
                log.debug(f"heartbeat_loop: RTT = {rtt_ms:.1f} ms")
                self.app.call_from_thread(
                    self.query_one("#rtt", Label).update,
                    f"RTT: {rtt_ms:.0f} ms"
                )
            except Exception as e:
                log.exception("heartbeat_loop: crashed")
                break

        log.info("heartbeat_loop: exited")

    # ── Button handlers ──────────────────────────────────────────────────
    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        log.info(f"Button pressed: {bid}")

        if bid == "play_btn":
            self.do_play()
        elif bid == "pause_btn":
            log.info("Sending PAUSE")
            send_json(conn, {"command": "PAUSE", "token": token})
        elif bid == "resume_btn":
            log.info("Sending RESUME")
            send_json(conn, {"command": "RESUME", "token": token})
        elif bid == "stop_btn":
            log.info("Sending STOP")
            send_json(conn, {"command": "STOP", "token": token})
        elif bid == "create_playlist_btn":
            self.do_create_playlist()
        elif bid == "load_playlist_btn":
            self.do_load_playlist()
        elif bid == "download_btn":
            self.do_download()

    def do_play(self):
        global now_playing
        lv       = self.query_one("#song_list", ListView)
        selected = lv.highlighted_child

        if selected is None:
            log.warning("do_play: no song selected")
            self.log_msg("Select a song first.")
            return

        index       = lv.index
        song        = songs[index]
        now_playing = song

        log.info(f"do_play: song_id={song['song_id']} title='{song['title']}' artist='{song['artist']}'")

        self.query_one("#now_playing", Static).update(
            f"▶  {song['title']}  —  {song['artist']}"
        )
        send_json(conn, {
            "command": "PLAY",
            "song_id": song["song_id"],
            "token":   token,
        })
        self.log_msg(f"Playing: {song['title']}")

    def do_download(self):
        links_file = self.query_one("#download_input", Input).value.strip()
        log.info(f"do_download: links_file='{links_file}'")

        if not links_file:
            log.warning("do_download: no file path entered")
            self.log_msg("Enter the path to your links.txt file.")
            return

        if not os.path.exists(links_file):
            log.error(f"do_download: file not found: {links_file}")
            self.log_msg(f"File not found: {links_file}")
            return

        with open(links_file) as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

        log.info(f"do_download: found {len(urls)} URLs in file")

        if not urls:
            log.warning("do_download: file was empty")
            self.log_msg("No URLs found in file.")
            return

        send_json(conn, {
            "command": "DOWNLOAD",
            "token":   token,
            "urls":    urls,
        })
        self.log_msg(f"Sent {len(urls)} URLs to server.")

    # ── UI log helper ────────────────────────────────────────────────────
    def log_msg(self, text):
        log.info(f"[UI] {text}")
        try:
            self.app.call_from_thread(
                self.query_one("#log_box", Log).write_line,
                text
            )
        except Exception as e:
            log.warning(f"log_msg: could not update UI log box: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════
class MusicApp(App):
    TITLE    = "DeltaPlay"
    CSS_PATH = None

    BINDINGS = [
        ("q", "quit",   "Quit"),
        ("p", "pause",  "Pause"),
        ("r", "resume", "Resume"),
        ("s", "stop",   "Stop"),
    ]

    def on_mount(self):
        log.info("MusicApp started")
        self.push_screen(LoginScreen())

    def action_pause(self):
        if conn and token:
            log.info("Keybind: PAUSE")
            send_json(conn, {"command": "PAUSE", "token": token})

    def action_resume(self):
        if conn and token:
            log.info("Keybind: RESUME")
            send_json(conn, {"command": "RESUME", "token": token})

    def action_stop(self):
        if conn and token:
            log.info("Keybind: STOP")
            send_json(conn, {"command": "STOP", "token": token})

    def on_unmount(self):
        log.info("MusicApp shutting down")
        if conn:
            try:
                conn.close()
                log.info("Socket closed cleanly")
            except Exception as e:
                log.warning(f"Error closing socket: {e}")


if __name__ == "__main__":
    log.info("=== DeltaPlay client starting ===")
    MusicApp().run()
    log.info("=== DeltaPlay client exited ===")

