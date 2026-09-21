#!/usr/bin/env python3
"""Server web multiplayer per L'intesa vincente."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web


ROOT = Path(__file__).resolve().parent
RES = ROOT / "res"
WEB = ROOT / "web"
MAX_PLAYERS = 3
ROUND_SECONDS = 60
FEEDBACK_SECONDS = 0.54
INITIAL_ROLE_SEATS = {"controller": 1, "helper": 2, "guesser": 3}


def read_words(path: Path) -> list[str]:
    try:
        words = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as error:
        raise RuntimeError(f"Impossibile leggere {path.name}: {error.strerror}") from error
    if not words:
        raise RuntimeError(f"{path.name} non contiene parole.")
    return words


def score_after_answer(score: int, correct: bool, doubled: bool) -> int:
    change = 2 if doubled else 1
    return score + change if correct else max(0, score - change)


def clean_name(value: object) -> str:
    return " ".join(str(value).split())[:24]


def seat_for_initial_role(value: object) -> int | None:
    if value == "spectator":
        return None
    if isinstance(value, str) and value in INITIAL_ROLE_SEATS:
        return INITIAL_ROLE_SEATS[value]
    raise ValueError("Scegli uno dei tre ruoli iniziali oppure spettatore.")


@dataclass
class Session:
    token: str
    name: str
    seat: int | None = None
    socket: web.WebSocketResponse | None = None


class GameRoom:
    """Una squadra da tre: due suggeritori e un indovino a ruoli rotanti."""

    def __init__(self, words: list[str] | None = None, double_words: list[str] | None = None) -> None:
        self.words = words if words is not None else read_words(RES / "parole.txt")
        self.double_words = double_words if double_words is not None else read_words(RES / "paroleRaddoppio.txt")
        self.rng = random.SystemRandom()
        self.sessions: dict[str, Session] = {}
        self.players: list[str | None] = [None] * MAX_PLAYERS
        self.lock = asyncio.Lock()
        self.timer_task: asyncio.Task[None] | None = None
        self.feedback_task: asyncio.Task[None] | None = None
        self.deadline: float | None = None
        self._reset_state()

    def _team_ready(self) -> bool:
        return all(self.players)

    def _seat_name(self, seat: int) -> str:
        token = self.players[seat - 1]
        session = self.sessions.get(token) if token else None
        return session.name if session else f"Giocatore {seat}"

    def _turn_roles(self) -> dict[str, int]:
        """Il primo suggeritore conserva i comandi quando non è l'indovino."""
        guesser = self.guesser_seat
        controller = 1 if guesser != 1 else 2
        helper = next(seat for seat in range(1, MAX_PLAYERS + 1) if seat not in (controller, guesser))
        return {"controller": controller, "helper": helper, "guesser": guesser}

    def _turn_role(self, seat: int) -> str:
        for role, role_seat in self._turn_roles().items():
            if seat == role_seat:
                return role
        raise ValueError("Posto giocatore non valido.")

    def _controller_name(self) -> str:
        return self._seat_name(self._turn_roles()["controller"])

    def _lobby_status(self) -> str:
        missing = sum(token is None for token in self.players)
        if missing:
            return f"In attesa di {missing} {'giocatore' if missing == 1 else 'giocatori'}."
        return f"Squadra al completo. {self._controller_name()} ha i comandi."

    def _reset_state(self) -> None:
        self._stop_timer()
        self._stop_feedback()
        self.phase = "idle"
        self.remaining = ROUND_SECONDS
        self.score = 0
        self.correct = 0
        self.wrong = 0
        self.passes = 0
        self.doubles = 0
        self.round = 1
        self.guesser_seat = 3
        self.active_double = False
        self.started = False
        self.word = ""
        self.feedback = "normal"
        self.status = self._lobby_status()
        self.used_words: set[str] = set()
        self.used_double: set[str] = set()

    def _stop_timer(self) -> None:
        task = self.timer_task
        self.timer_task = None
        self.deadline = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _stop_feedback(self) -> None:
        task = self.feedback_task
        self.feedback_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _pick_word(self, words: list[str], used: set[str]) -> str:
        # ponytail: linear filtering is instant for the bundled lists; add shuffled decks only if they grow large.
        choices = [word for word in words if word not in used]
        if not choices:
            used.clear()
            choices = words
        word = self.rng.choice(choices)
        used.add(word)
        return word

    def _role(self, session: Session) -> tuple[str, int | None]:
        if session.seat is not None and self.players[session.seat - 1] == session.token:
            return self._turn_role(session.seat), session.seat
        return "spectator", None

    def _active_word(self) -> bool:
        return self.phase in {"running", "stopped", "feedback"}

    def _snapshot(self, session: Session) -> dict[str, object]:
        role, seat = self._role(session)
        can_control = role == "controller" and self._team_ready()
        can_see_word = role in {"controller", "helper"}
        if self._active_word():
            visible_word = self.word if can_see_word else None
        else:
            visible_word = "Premi Spazio" if can_see_word else None
        roles = self._turn_roles()
        player_slots: list[dict[str, object] | None] = []
        for index, token in enumerate(self.players):
            player = self.sessions.get(token) if token else None
            player_slots.append(
                {
                    "seat": index + 1,
                    "name": player.name if player else "Posto libero",
                    "turn_role": self._turn_role(index + 1) if player else None,
                }
                if player
                else None
            )
        spectators = sum(
            1
            for candidate in self.sessions.values()
            if candidate.socket is not None and candidate.seat is None
        )
        return {
            "type": "state",
            "you": {
                "role": role,
                "seat": seat,
                "can_control": can_control,
                "can_see_word": can_see_word,
            },
            "room": {
                "phase": self.phase,
                "started": self.started,
                "remaining": self.remaining,
                "score": self.score,
                "correct": self.correct,
                "wrong": self.wrong,
                "passes": self.passes,
                "doubles": self.doubles,
                "round": self.round,
                "word": visible_word,
                "word_hidden": self._active_word() and not can_see_word,
                "feedback": self.feedback,
                "status": self.status,
                "players": player_slots,
                "controller_seat": roles["controller"],
                "helper_seat": roles["helper"],
                "guesser_seat": roles["guesser"],
                "spectators": spectators,
            },
        }

    async def _broadcast(self) -> None:
        messages = [
            session.socket.send_json(self._snapshot(session))
            for session in self.sessions.values()
            if session.socket is not None and not session.socket.closed
        ]
        if messages:
            await asyncio.gather(*messages, return_exceptions=True)

    async def _send_error(self, session: Session, message: str) -> None:
        if session.socket is not None and not session.socket.closed:
            await session.socket.send_json({"type": "error", "message": message})

    async def join(self, socket: web.WebSocketResponse, name: str, token: object, seat: int | None) -> Session:
        async with self.lock:
            session = self.sessions.get(token) if isinstance(token, str) else None
            if session is None:
                if seat is not None and self.players[seat - 1] is not None:
                    raise ValueError("Questo ruolo iniziale è già occupato.")
                session = Session(secrets.token_urlsafe(32), name, seat=seat)
                self.sessions[session.token] = session
                if seat is not None:
                    self.players[seat - 1] = session.token
            else:
                if session.seat != seat:
                    raise ValueError("Il tuo ruolo è già stato scelto per questa connessione.")
                if session.socket is not None and session.socket is not socket and not session.socket.closed:
                    await session.socket.close(code=4001, message=b"Connection replaced")
                session.name = name
            session.socket = socket
            if not self.started:
                self.status = self._lobby_status()
            await socket.send_json({"type": "joined", "token": session.token})
            await self._broadcast()
            return session

    async def leave(self, session: Session, socket: web.WebSocketResponse) -> None:
        async with self.lock:
            if session.socket is not socket:
                return
            session.socket = None
            was_player = session.seat is not None and self.players[session.seat - 1] == session.token
            if was_player:
                self.players[session.seat - 1] = None
                session.seat = None
                self._reset_state()
            self.sessions.pop(session.token, None)
            if not self.sessions:
                self._reset_state()
            await self._broadcast()

    def _sync_clock(self) -> bool:
        if self.phase != "running" or self.deadline is None:
            return False
        now = asyncio.get_running_loop().time()
        self.remaining = max(0, math.ceil(self.deadline - now))
        if self.remaining:
            return False
        self._finish()
        return True

    def _start_timer(self) -> None:
        self._stop_timer()
        self.deadline = asyncio.get_running_loop().time() + self.remaining
        self.timer_task = asyncio.create_task(self._timer_loop())

    async def _timer_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.1)
                async with self.lock:
                    if self.phase != "running":
                        return
                    previous = self.remaining
                    finished = self._sync_clock()
                    if finished or self.remaining != previous:
                        await self._broadcast()
                    if finished:
                        return
        except asyncio.CancelledError:
            return

    def _finish(self) -> None:
        self._stop_timer()
        self._stop_feedback()
        self.phase = "idle"
        self.remaining = ROUND_SECONDS
        self.active_double = False
        self.feedback = "normal"
        self.word = ""
        self.round += 1
        self.guesser_seat = self.guesser_seat % MAX_PLAYERS + 1
        self.status = f"Tempo scaduto. Turno {self.round}: {self._controller_name()} ha i comandi."

    def _reveal_word(self, actor: str) -> None:
        doubled = self.phase == "double-ready"
        words = self.double_words if doubled else self.words
        used = self.used_double if doubled else self.used_words
        self.active_double = doubled
        self.word = self._pick_word(words, used)
        self.phase = "running"
        self.started = True
        self.status = f"{actor} ha avviato la parola — Spazio per fermare il tempo."
        self._start_timer()

    def _answer(self, correct: bool, actor: str, status: str | None = None) -> None:
        self.score = score_after_answer(self.score, correct, self.active_double)
        self.correct += int(correct)
        self.wrong += int(not correct)
        self.active_double = False
        self.phase = "feedback"
        self.feedback = "correct" if correct else "wrong"
        self.status = status or f"{actor}: risposta {'giusta' if correct else 'errata'}."
        self._stop_feedback()
        self.feedback_task = asyncio.create_task(self._feedback_loop())

    def _ready_for_next_word(self) -> None:
        self.phase = "idle"
        self.feedback = "normal"
        self.word = ""
        self.status = f"{self._controller_name()} continua: premi Spazio per la parola successiva."

    async def _feedback_loop(self) -> None:
        try:
            await asyncio.sleep(FEEDBACK_SECONDS)
            async with self.lock:
                if self.phase == "feedback":
                    self._ready_for_next_word()
                    self.feedback_task = None
                    await self._broadcast()
        except asyncio.CancelledError:
            return

    async def command(self, session: Session, action: object) -> None:
        if not isinstance(action, str):
            await self._send_error(session, "Comando non valido.")
            return
        async with self.lock:
            role, _seat = self._role(session)
            if not self._team_ready():
                await self._send_error(session, "Servono tutti e tre i giocatori per iniziare.")
                return
            if role != "controller":
                await self._send_error(session, f"In questo turno comanda solo {self._controller_name()}.")
                return
            if self._sync_clock():
                await self._broadcast()
                await self._send_error(session, "Il tempo è scaduto.")
                return

            actor = session.name
            if action == "restart":
                self._reset_state()
                self.status = f"{actor} ha ricominciato. {self._controller_name()} ha i comandi."
            elif action == "space":
                if self.phase in ("idle", "double-ready"):
                    self._reveal_word(actor)
                elif self.phase == "running":
                    self._stop_timer()
                    self.phase = "stopped"
                    self.status = f"{actor} ha fermato il tempo — scegli la risposta."
                else:
                    await self._send_error(session, "Questo comando non è disponibile ora.")
                    return
            elif action in ("correct", "wrong"):
                if self.phase != "stopped":
                    await self._send_error(session, "Ferma prima il tempo.")
                    return
                self._answer(action == "correct", actor)
            elif action == "pass":
                if self.phase != "stopped":
                    await self._send_error(session, "Il passo è disponibile solo a tempo fermo.")
                    return
                self.active_double = False
                if self.passes >= 3:
                    self._answer(False, actor, "Passi terminati: errore.")
                else:
                    self.passes += 1
                    self.word = ""
                    self.phase = "idle"
                    self.status = f"{actor} ha usato il passo {self.passes}/3."
            elif action == "double":
                if self.phase != "idle":
                    await self._send_error(session, "Il raddoppio si sceglie prima della parola.")
                    return
                if self.score < 2:
                    await self._send_error(session, "Servono almeno 2 punti per il raddoppio.")
                    return
                if self.doubles >= 2:
                    await self._send_error(session, "I due raddoppi sono già stati usati.")
                    return
                self.doubles += 1
                self.phase = "double-ready"
                self.status = f"{actor} ha scelto il raddoppio — premi Spazio per la frase."
            else:
                await self._send_error(session, "Comando sconosciuto.")
                return
            await self._broadcast()

    async def close(self) -> None:
        async with self.lock:
            self._stop_timer()
            self._stop_feedback()
            sockets = [session.socket for session in self.sessions.values() if session.socket is not None]
            self.sessions.clear()
            self.players = [None] * MAX_PLAYERS
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)


def allowed_origins() -> set[str]:
    return {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()}


async def home(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB / "index.html")


async def health(request: web.Request) -> web.Response:
    room: GameRoom = request.app["room"]
    return web.json_response({"ok": True, "players": sum(token is not None for token in room.players)})


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    origins = allowed_origins()
    origin = request.headers.get("Origin")
    if origins and origin not in origins:
        raise web.HTTPForbidden(text="Origine non consentita")

    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    room: GameRoom = request.app["room"]
    session: Session | None = None
    try:
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                await socket.send_json({"type": "error", "message": "Messaggio non valido."})
                continue
            if not isinstance(payload, dict):
                await socket.send_json({"type": "error", "message": "Messaggio non valido."})
                continue
            if session is None:
                if payload.get("type") != "join":
                    await socket.send_json({"type": "error", "message": "Entra prima nella stanza."})
                    continue
                name = clean_name(payload.get("name", ""))
                if not name:
                    await socket.send_json({"type": "error", "message": "Inserisci un nome."})
                    continue
                try:
                    seat = seat_for_initial_role(payload.get("initialRole"))
                    session = await room.join(socket, name, payload.get("token"), seat)
                except ValueError as error:
                    await socket.send_json({"type": "error", "message": str(error)})
            elif payload.get("type") == "action":
                await room.command(session, payload.get("action"))
    finally:
        if session is not None:
            await room.leave(session, socket)
    return socket


async def cleanup(app: web.Application) -> None:
    room: GameRoom = app["room"]
    await room.close()


def create_app() -> web.Application:
    app = web.Application()
    app["room"] = GameRoom()
    app.router.add_get("/", home)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static", WEB)
    app.router.add_static("/assets/img", RES / "img")
    app.router.add_static("/assets/lemon_milk", RES / "lemon_milk")
    app.router.add_static("/assets/sounds", RES / "sounds")
    app.on_cleanup.append(cleanup)
    return app


async def self_check() -> None:
    room = GameRoom(words=["uno"], double_words=["due"])
    controller = Session("controller", "Ada", seat=1)
    helper = Session("helper", "Bruno", seat=2)
    guesser = Session("guesser", "Clara", seat=3)
    spectator = Session("spectator", "Dino")
    room.sessions = {session.token: session for session in (controller, helper, guesser, spectator)}
    room.players = [controller.token, helper.token, guesser.token]

    assert seat_for_initial_role("controller") == 1
    assert seat_for_initial_role("spectator") is None
    assert room._role(controller) == ("controller", 1)
    assert room._role(helper) == ("helper", 2)
    assert room._role(guesser) == ("guesser", 3)
    await room.command(helper, "space")
    assert room.phase == "idle"
    await room.command(controller, "space")
    assert room.phase == "running" and room.word == "uno"
    assert room._snapshot(controller)["room"]["word"] == "uno"
    assert room._snapshot(helper)["room"]["word"] == "uno"
    assert room._snapshot(guesser)["room"]["word"] is None
    assert room._snapshot(spectator)["room"]["word"] is None
    await room.command(controller, "space")
    assert room.phase == "stopped"
    await room.command(controller, "correct")
    assert room.score == 1 and room.correct == 1 and room.feedback == "correct"
    room._stop_feedback()
    room._ready_for_next_word()
    room._finish()
    assert room.round == 2 and room._role(controller) == ("guesser", 1)
    assert room._role(helper) == ("controller", 2)
    assert room._role(guesser) == ("helper", 3)
    await room.command(guesser, "space")
    assert room.phase == "idle"
    await room.command(helper, "space")
    assert room.phase == "running"
    assert room._snapshot(controller)["room"]["word"] is None
    room._finish()
    assert room.round == 3 and room._role(controller) == ("controller", 1)
    await room.close()


def main() -> None:
    if "--self-test" in sys.argv:
        asyncio.run(self_check())
        print("ok")
        return
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
