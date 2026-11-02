import asyncio
import mimetypes
import os
from pathlib import Path

import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import websockets

ROOT = Path(__file__).resolve().parent
rooms = {}


class GameHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format, *args):
        print(format % args)


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
