import asyncio
import mimetypes
import os
from pathlib import Path

import threading
import json
import sqlite3
import hashlib
import secrets
import time
from urllib.parse import urlparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import websockets

ROOT = Path(__file__).resolve().parent
rooms = {}
DB_PATH = ROOT / "patling_accounts.sqlite3"


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            rank_points INTEGER NOT NULL DEFAULT 0,
            wallet_points INTEGER NOT NULL DEFAULT 0,
            owned_skins TEXT NOT NULL DEFAULT '{}',
            active_skins TEXT NOT NULL DEFAULT '{}',
            wins INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """)
        connection.execute("PRAGMA table_info(users)").fetchall()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000).hex()


def public_profile(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "rankPoints": row["rank_points"],
        "walletPoints": row["wallet_points"],
        "ownedSkins": json.loads(row["owned_skins"]),
        "activeSkins": json.loads(row["active_skins"]),
        "wins": row["wins"],
    }


class GameHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format, *args):
        print(format % args)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def current_user(self):
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token:
            return None
        with db() as connection:
            row = connection.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token=?",
                (token,),
            ).fetchone()
        return row

    def do_POST(self):
        try:
            data = self.body()
            path = urlparse(self.path).path

            if path == "/api/register":
                username = str(data.get("username", "")).strip()
                password = str(data.get("password", ""))
                if not 3 <= len(username) <= 24:
                    return self.send_json({"error": "Username must be 3-24 characters."}, 400)
                if len(password) < 6:
                    return self.send_json({"error": "Password must be at least 6 characters."}, 400)
                salt = secrets.token_hex(16)
                try:
                    with db() as connection:
                        cursor = connection.execute(
                            "INSERT INTO users(username,password_hash,salt,created_at) VALUES(?,?,?,?)",
                            (username, password_hash(password, salt), salt, int(time.time())),
                        )
                        user_id = cursor.lastrowid
                except sqlite3.IntegrityError:
                    return self.send_json({"error": "That username is already registered."}, 409)
                return self.login_user(user_id)

            if path == "/api/login":
                username = str(data.get("username", "")).strip()
                password = str(data.get("password", ""))
                with db() as connection:
                    row = connection.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
                if row is None or password_hash(password, row["salt"]) != row["password_hash"]:
                    return self.send_json({"error": "Incorrect username or password."}, 401)
                return self.login_user(row["id"])

            user = self.current_user()
            if user is None:
                return self.send_json({"error": "Please log in first."}, 401)

            if path == "/api/profile":
                return self.send_json({"user": public_profile(user), "token": self.headers.get("Authorization", "").removeprefix("Bearer ").strip()})

            if path == "/api/win":
                reward = max(0, min(500, int(data.get("reward", 100))))
                with db() as connection:
                    connection.execute(
                        "UPDATE users SET rank_points=rank_points+?, wallet_points=wallet_points+?, wins=wins+1 WHERE id=?",
                        (reward, reward, user["id"]),
                    )
                    row = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                return self.send_json({"user": public_profile(row)})

            if path in ("/api/buy-skin", "/api/equip-skin"):
                fighter = str(data.get("fighter", "")).strip().lower()
                skin = str(data.get("skin", "")).strip().lower()
                if not fighter or not skin:
                    return self.send_json({"error": "Fighter and skin are required."}, 400)
                owned = json.loads(user["owned_skins"])
                active = json.loads(user["active_skins"])
                if path == "/api/buy-skin":
                    prices = {"default": 0, "ember": 150, "neon": 250, "champion": 400}
                    if skin not in prices:
                        return self.send_json({"error": "Unknown skin."}, 400)
                    fighter_owned = owned.get(fighter, ["default"])
                    if skin in fighter_owned:
                        return self.send_json({"user": public_profile(user), "message": "Already owned."})
                    if user["wallet_points"] < prices[skin]:
                        return self.send_json({"error": f"You need {prices[skin]} points for this skin."}, 400)
                    fighter_owned.append(skin)
                    owned[fighter] = fighter_owned
                    with db() as connection:
                        connection.execute(
                            "UPDATE users SET wallet_points=wallet_points-?, owned_skins=? WHERE id=?",
                            (prices[skin], json.dumps(owned), user["id"]),
                        )
                        row = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                    return self.send_json({"user": public_profile(row)})

                if skin != "default" and skin not in owned.get(fighter, ["default"]):
                    return self.send_json({"error": "Skin is not owned."}, 400)
                active[fighter] = skin
                with db() as connection:
                    connection.execute("UPDATE users SET active_skins=? WHERE id=?", (json.dumps(active), user["id"]))
                    row = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                return self.send_json({"user": public_profile(row)})

            return self.send_json({"error": "Unknown API action."}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": "Server error.", "detail": str(exc)}, 500)

    def login_user(self, user_id):
        token = secrets.token_urlsafe(32)
        with db() as connection:
            connection.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, user_id, int(time.time())))
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        self.send_json({"token": token, "user": public_profile(row)})


async def relay(websocket):
    room = None
    player = None
    try:
        async for message in websocket:
            import json
            data = json.loads(message)
            if data.get("type") == "join":
                room = str(data.get("room", "")).upper()
                rooms.setdefault(room, set())
                if len(rooms[room]) >= 2:
                    await websocket.send(json.dumps({"type": "error", "message": "Room is full"}))
                    continue
                websocket.role = "host" if not rooms[room] else "guest"
                websocket.player_room = room
                rooms[room].add(websocket)
                player = websocket.role
                await websocket.send(json.dumps({"type": "joined", "role": player}))
                for peer in rooms[room]:
                    if peer != websocket:
                        await peer.send(json.dumps({"type": "peer-connected", "role": player}))
                continue

            if room in rooms:
                for peer in list(rooms[room]):
                    if peer != websocket:
                        await peer.send(message)
    except Exception as exc:
        print("Relay error:", exc)
    finally:
        if room in rooms:
            rooms[room].discard(websocket)
            if not rooms[room]:
                rooms.pop(room, None)


async def main():
    init_db()
    ws_port = int(os.environ.get("WS_PORT", "8765"))
    http_port = int(os.environ.get("PORT", "8764"))

    def serve_http():
        server = ThreadingHTTPServer(("0.0.0.0", http_port), GameHandler)
        print(f"Game page: http://localhost:{http_port}", flush=True)
        server.serve_forever()

    threading.Thread(target=serve_http, daemon=True).start()
    async with websockets.serve(relay, "0.0.0.0", ws_port):
        print(f"LAN relay: ws://0.0.0.0:{ws_port}/game", flush=True)
        print(f"On another phone: http://THIS-PC-LAN-IP:{http_port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
