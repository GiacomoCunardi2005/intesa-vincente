#!/usr/bin/env python3
"""Server web multiplayer per L'intesa vincente."""

from __future__ import annotations

import asyncio
from ipaddress import ip_address
import json
import math
import os
import random
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web


ROOT = Path(__file__).resolve().parent
RES = ROOT / "res"
WEB = ROOT / "web"
MAX_PLAYERS = 3
ROUND_SECONDS = 60
MAX_TURNS = 3
FEEDBACK_SECONDS = 0.54
DISCONNECT_GRACE_SECONDS = 30
INITIAL_ROLE_SEATS = {"controller": 1, "helper": 2, "guesser": 3}
RECORDS_PATH = Path(os.getenv("RECORDS_PATH", str(ROOT / "data" / "records.json")))


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


def ordered_records(records: list[tuple[str, int]]) -> list[tuple[str, int]]:
    return sorted(records, key=lambda record: (-record[1], record[0].casefold()))


def read_records(path: Path) -> list[tuple[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    records: list[tuple[str, int]] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        team = " ".join(record.get("team", "").split()) if isinstance(record.get("team"), str) else ""
        correct = record.get("correct")
        if team and type(correct) is int and correct >= 0:
            records.append((team[:78], correct))
    return ordered_records(records)


def write_records(path: Path, records: list[tuple[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = [{"team": team, "correct": correct} for team, correct in records]
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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
    disconnect_task: asyncio.Task[None] | None = None
    disconnect_generation: int = 0
    last_seen: float | None = None


class GameRoom:
    """Una squadra da tre: due suggeritori e un indovino a ruoli rotanti."""

    def __init__(
        self,
        words: list[str] | None = None,
        double_words: list[str] | None = None,
        records_path: Path | None = None,
    ) -> None:
        self.words = words if words is not None else read_words(RES / "parole.txt")
        self.double_words = double_words if double_words is not None else read_words(RES / "paroleRaddoppio.txt")
        self.records_path = records_path or RECORDS_PATH
        self.records = read_records(self.records_path)
        self.rng = random.SystemRandom()
        self.sessions: dict[str, Session] = {}
        self.players: list[str | None] = [None] * MAX_PLAYERS
        self.lock = asyncio.Lock()
        self.timer_task: asyncio.Task[None] | None = None
        self.feedback_task: asyncio.Task[None] | None = None
        self.reveal_task: asyncio.Task[None] | None = None
        self.disconnect_grace_seconds = DISCONNECT_GRACE_SECONDS
        self.deadline: float | None = None
        self.closing = False
        self._reset_state()

    def _team_ready(self) -> bool:
        return all(self.players)

    def _is_player(self, session: Session) -> bool:
        return session.seat is not None and self.players[session.seat - 1] == session.token

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
        self._stop_reveal()
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

    def _stop_reveal(self) -> None:
        task = self.reveal_task
        self.reveal_task = None
        self.guesser_reveal = None
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
        if self._is_player(session):
            return self._turn_role(session.seat), session.seat
        return "spectator", None

    def _cancel_disconnect(self, session: Session) -> None:
        session.disconnect_generation += 1
        task = session.disconnect_task
        session.disconnect_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _disconnect_delay(self, session: Session) -> float:
        if session.last_seen is None:
            return self.disconnect_grace_seconds
        elapsed = asyncio.get_running_loop().time() - session.last_seen
        return max(0, self.disconnect_grace_seconds - elapsed)

    def _make_spectator(self, session: Session, reason: str) -> bool:
        if not self._is_player(session):
            return False
        seat = session.seat
        assert seat is not None
        self.players[seat - 1] = None
        session.seat = None
        self._reset_state()
        self.status = f"{session.name} {reason}. Partita terminata. {self._lobby_status()}"
        return True

    def _active_word(self) -> bool:
        return self.phase in {"running", "stopped", "feedback"}

    def _snapshot(self, session: Session) -> dict[str, object]:
        role, seat = self._role(session)
        can_control = role == "controller" and self._team_ready()
        guesser_reveal = self.guesser_reveal if role == "guesser" else None
        can_see_word = role in {"controller", "helper"} or guesser_reveal is not None
        if self.phase == "finished":
            visible_word = "Partita conclusa"
        elif guesser_reveal is not None:
            visible_word = guesser_reveal
        elif self._active_word():
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
                    "connected": bool(player and player.socket is not None and not player.socket.closed),
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
                "records": [{"team": team, "correct": correct} for team, correct in self.records],
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
            if self.closing:
                raise ValueError("Server in riavvio: riprova tra poco.")
            session = self.sessions.get(token) if isinstance(token, str) else None
            is_returning = session is not None
            if session is None:
                if seat is not None and self.players[seat - 1] is not None:
                    raise ValueError("Questo ruolo iniziale è già occupato.")
                session = Session(secrets.token_urlsafe(32), name, seat=seat)
                self.sessions[session.token] = session
                if seat is not None:
                    self.players[seat - 1] = session.token
            else:
                # Il ruolo iniziale vale solo al primo ingresso: il token conserva
                # il posto attuale anche dopo una riconnessione.
                self._cancel_disconnect(session)
                if session.socket is not None and session.socket is not socket and not session.socket.closed:
                    await session.socket.close(code=4001, message=b"Connection replaced")
                session.name = name
            session.socket = socket
            session.last_seen = asyncio.get_running_loop().time()
            if not self.started:
                self.status = self._lobby_status()
            elif is_returning and session.seat is not None:
                self.status = f"{session.name} è rientrato nella squadra."
            await socket.send_json({"type": "joined", "token": session.token})
            await self._broadcast()
            return session

    async def spectate(self, session: Session, socket: web.WebSocketResponse) -> None:
        async with self.lock:
            if session.socket is not socket:
                return
            self._cancel_disconnect(session)
            if not self._make_spectator(session, "è diventato spettatore"):
                await self._send_error(session, "Sei già spettatore.")
                return
            await self._broadcast()

    async def claim_seat(self, session: Session, socket: web.WebSocketResponse, seat: object) -> None:
        async with self.lock:
            if self.closing or session.socket is not socket:
                return
            if type(seat) is not int or not 1 <= seat <= MAX_PLAYERS:
                await self._send_error(session, "Posto giocatore non valido.")
                return
            if self._is_player(session):
                await self._send_error(session, "Diventa prima spettatore per cambiare posto.")
                return
            if self.players[seat - 1] is not None:
                await self._send_error(session, "Questo posto è già occupato.")
                return
            self.players[seat - 1] = session.token
            session.seat = seat
            if not self.started:
                self.status = self._lobby_status()
            await self._broadcast()

    async def _expire_disconnected_player(self, token: str, generation: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self.lock:
                session = self.sessions.get(token)
                if (
                    session is None
                    or session.disconnect_generation != generation
                    or session.socket is not None
                    or not self._is_player(session)
                ):
                    return
                session.disconnect_task = None
                self._make_spectator(session, f"non è rientrato entro {DISCONNECT_GRACE_SECONDS} secondi ed è diventato spettatore")
                # ponytail: keep the token so a late reconnect remains a spectator; prune idle sessions if room churn grows.
                await self._broadcast()
        except asyncio.CancelledError:
            return

    async def disconnect(self, session: Session, socket: web.WebSocketResponse) -> None:
        async with self.lock:
            if session.socket is not socket:
                return
            session.socket = None
            if self._is_player(session):
                self._cancel_disconnect(session)
                generation = session.disconnect_generation
                session.disconnect_task = asyncio.create_task(
                    self._expire_disconnected_player(session.token, generation, self._disconnect_delay(session))
                )
                self.status = f"{session.name} è disconnesso: attendo {DISCONNECT_GRACE_SECONDS} secondi per il rientro."
            else:
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

    def _finish(self, actor: str | None = None) -> None:
        if self.phase == "finished":
            return
        self._stop_timer()
        self._stop_feedback()
        self._stop_reveal()
        self.phase = "idle"
        self.remaining = ROUND_SECONDS
        self.active_double = False
        self.feedback = "normal"
        self.word = ""
        if actor is not None or self.round >= MAX_TURNS:
            self.phase = "finished"
            self.remaining = 0
            saved = self._save_record()
            if actor is None:
                self.status = f"Tempo scaduto. Partita finita: {self.correct} parole indovinate in {MAX_TURNS} turni."
            else:
                self.status = f"{actor} ha concluso la partita: {self.correct} parole indovinate."
            self.status += " Record salvato." if saved else " Impossibile salvare il record."
            return
        self.round += 1
        self.guesser_seat = self.guesser_seat % MAX_PLAYERS + 1
        self.status = f"Tempo scaduto. Turno {self.round}: {self._controller_name()} ha i comandi."

    def _save_record(self) -> bool:
        if not self._team_ready():
            return False
        team = " - ".join(self._seat_name(seat) for seat in range(1, MAX_PLAYERS + 1))
        records = ordered_records([*self.records, (team, self.correct)])
        try:
            write_records(self.records_path, records)
        except OSError:
            return False
        self.records = records
        return True

    def _reveal_word(self, actor: str) -> None:
        doubled = self.phase == "double-ready"
        words = self.double_words if doubled else self.words
        used = self.used_double if doubled else self.used_words
        self.active_double = doubled
        self.word = self._pick_word(words, used)
        self.phase = "running"
        self.started = True
        self.status = f"{actor} ha avviato la parola — l'indovino preme Spazio per fermare il tempo."
        self._start_timer()

    def _answer(self, correct: bool, actor: str, status: str | None = None) -> None:
        self._reveal_guesser()
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
        self._stop_reveal()
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

    def _reveal_guesser(self) -> None:
        self._stop_reveal()
        self.guesser_reveal = self.word
        self.reveal_task = asyncio.create_task(self._reveal_loop())

    async def _reveal_loop(self) -> None:
        try:
            await asyncio.sleep(FEEDBACK_SECONDS)
            async with self.lock:
                self.guesser_reveal = None
                self.reveal_task = None
                await self._broadcast()
        except asyncio.CancelledError:
            return

    async def command(self, session: Session, socket: web.WebSocketResponse, action: object) -> None:
        async with self.lock:
            if session.socket is not socket:
                return
            if not isinstance(action, str):
                await self._send_error(session, "Comando non valido.")
                return
            role, _seat = self._role(session)
            if not self._team_ready():
                await self._send_error(session, "Servono tutti e tre i giocatori per iniziare.")
                return
            can_stop_time = action == "space" and self.phase == "running" and role == "guesser"
            if role != "controller" and not can_stop_time:
                await self._send_error(session, f"In questo turno comanda solo {self._controller_name()}.")
                return
            if self._sync_clock():
                await self._broadcast()
                await self._send_error(session, "Il tempo è scaduto.")
                return

            actor = session.name
            if action == "finish":
                if not self.started or self.phase == "finished":
                    await self._send_error(session, "Non c'è una partita da concludere.")
                    return
                self._finish(actor)
            elif action == "space":
                if self.phase in ("idle", "double-ready"):
                    self._reveal_word(actor)
                elif self.phase == "running":
                    if role != "guesser":
                        await self._send_error(session, "Durante il tempo può fermare solo l'indovino.")
                        return
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
                if self.phase not in ("running", "stopped"):
                    await self._send_error(session, "Il passo è disponibile solo durante una parola.")
                    return
                self.active_double = False
                if self.passes >= 3:
                    self._stop_timer()
                    self._answer(False, actor, "Passi terminati: errore.")
                else:
                    self.passes += 1
                    self._reveal_guesser()
                    if self.phase == "running":
                        self.word = self._pick_word(self.words, self.used_words)
                        self.status = f"{actor} ha usato il passo {self.passes}/3 — tempo in corso."
                    else:
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
            self.closing = True
            self._stop_timer()
            self._stop_feedback()
            self._stop_reveal()
            sockets = [session.socket for session in self.sessions.values() if session.socket is not None]
            for session in self.sessions.values():
                self._cancel_disconnect(session)
            self.sessions.clear()
            self.players = [None] * MAX_PLAYERS
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)


def allowed_origins() -> set[str]:
    return {origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()}


def origin_is_allowed(origin: str | None, scheme: str, host: str, origins: set[str]) -> bool:
    if not origins or origin in origins:
        return True
    parsed = urlsplit(origin or "")
    try:
        ip_address(parsed.hostname or "")
    except ValueError:
        return False
    return origin == f"{scheme}://{host}"


async def home(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB / "index.html")


async def health(request: web.Request) -> web.Response:
    room: GameRoom = request.app["room"]
    return web.json_response({"ok": True, "players": sum(token is not None for token in room.players)})


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    origins = allowed_origins()
    origin = request.headers.get("Origin")
    if not origin_is_allowed(origin, request.scheme, request.host, origins):
        raise web.HTTPForbidden(text="Origine non consentita")

    socket = web.WebSocketResponse(heartbeat=10, autoping=False)
    await socket.prepare(request)
    room: GameRoom = request.app["room"]
    session: Session | None = None
    try:
        async for message in socket:
            if session is not None and session.socket is socket:
                session.last_seen = asyncio.get_running_loop().time()
            if message.type == WSMsgType.PING:
                await socket.pong(message.data)
                continue
            if message.type == WSMsgType.PONG:
                continue
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
                await room.command(session, socket, payload.get("action"))
            elif payload.get("type") == "spectate":
                await room.spectate(session, socket)
            elif payload.get("type") == "claim-seat":
                await room.claim_seat(session, socket, payload.get("seat"))
            else:
                await socket.send_json({"type": "error", "message": "Messaggio sconosciuto."})
    finally:
        if session is not None:
            await room.disconnect(session, socket)
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
    class TestSocket:
        def __init__(self) -> None:
            self.closed = False
            self.messages: list[dict[str, object]] = []

        async def send_json(self, payload: dict[str, object]) -> None:
            self.messages.append(payload)

        async def close(self, *_args: object, **_kwargs: object) -> None:
            self.closed = True

    temporary_records = tempfile.TemporaryDirectory()
    records_path = Path(temporary_records.name) / "records.json"
    room = GameRoom(words=["uno"], double_words=["due"], records_path=records_path)
    controller_socket = TestSocket()
    helper_socket = TestSocket()
    guesser_socket = TestSocket()
    spectator_socket = TestSocket()
    controller = Session("controller", "Ada", seat=1, socket=controller_socket)  # type: ignore[arg-type]
    helper = Session("helper", "Bruno", seat=2, socket=helper_socket)  # type: ignore[arg-type]
    guesser = Session("guesser", "Clara", seat=3, socket=guesser_socket)  # type: ignore[arg-type]
    spectator = Session("spectator", "Dino", socket=spectator_socket)  # type: ignore[arg-type]
    room.sessions = {session.token: session for session in (controller, helper, guesser, spectator)}
    room.players = [controller.token, helper.token, guesser.token]

    assert seat_for_initial_role("controller") == 1
    assert seat_for_initial_role("spectator") is None
    origins = {"https://intesa.cunardi.com"}
    assert origin_is_allowed("https://intesa.cunardi.com", "http", "192.168.1.10:5522", origins)
    assert origin_is_allowed("http://192.168.1.10:5522", "http", "192.168.1.10:5522", origins)
    assert not origin_is_allowed("http://192.168.1.11:5522", "http", "192.168.1.10:5522", origins)
    assert not origin_is_allowed("https://example.com", "https", "intesa.cunardi.com", origins)
    assert room._role(controller) == ("controller", 1)
    assert room._role(helper) == ("helper", 2)
    assert room._role(guesser) == ("guesser", 3)
    controller.last_seen = asyncio.get_running_loop().time() - 10
    assert 19 <= room._disconnect_delay(controller) <= 20
    controller.last_seen = None
    await room.command(helper, helper_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "idle"
    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "running" and room.word == "uno"
    assert room._snapshot(controller)["room"]["word"] == "uno"
    assert room._snapshot(helper)["room"]["word"] == "uno"
    assert room._snapshot(guesser)["room"]["word"] is None
    assert room._snapshot(spectator)["room"]["word"] is None
    deadline = room.deadline
    await room.command(controller, controller_socket, "pass")  # type: ignore[arg-type]
    assert room.phase == "running" and room.passes == 1 and room.word == "uno" and room.deadline == deadline
    assert room._snapshot(guesser)["room"]["word"] == "uno"
    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "running"
    await room.command(guesser, guesser_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "stopped"
    await room.command(controller, controller_socket, "pass")  # type: ignore[arg-type]
    assert room.phase == "idle" and room._snapshot(guesser)["room"]["word"] == "uno"
    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    await room.command(guesser, guesser_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "stopped"
    await room.command(controller, controller_socket, "correct")  # type: ignore[arg-type]
    assert room.score == 1 and room.correct == 1 and room.feedback == "correct"
    assert room._snapshot(guesser)["room"]["word"] == "uno"
    room._stop_feedback()
    room._ready_for_next_word()
    assert room._snapshot(guesser)["room"]["word"] is None
    room._finish()
    assert room.round == 2 and room._role(controller) == ("guesser", 1)
    assert room._role(helper) == ("controller", 2)
    assert room._role(guesser) == ("helper", 3)
    await room.command(guesser, guesser_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "idle"
    await room.command(helper, helper_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "running"
    assert room._snapshot(controller)["room"]["word"] is None
    room._finish()
    assert room.round == 3 and room._role(controller) == ("controller", 1)

    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    assert room.started and room.phase == "running"
    room.correct = 15
    room.score = 4
    room._finish()
    assert room.phase == "finished" and room.round == MAX_TURNS
    assert room.records == [("Ada - Bruno - Clara", 15)]
    assert room._snapshot(spectator)["room"]["records"] == [{"team": "Ada - Bruno - Clara", "correct": 15}]
    saved = GameRoom(words=["uno"], double_words=["due"], records_path=records_path)
    assert saved.records == [("Ada - Bruno - Clara", 15)]
    await saved.close()
    room._finish()
    assert room.records == [("Ada - Bruno - Clara", 15)]
    room._reset_state()
    assert not room.started and room.phase == "idle" and room.records == [("Ada - Bruno - Clara", 15)]

    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    assert room.started and room.phase == "running"
    await room.spectate(controller, controller_socket)  # type: ignore[arg-type]
    assert controller.seat is None and room.players[0] is None
    assert not room.started and room.phase == "idle" and room.score == 0
    assert room.records == [("Ada - Bruno - Clara", 15)]
    assert room._snapshot(controller)["room"]["word"] is None
    await room.claim_seat(controller, controller_socket, 1)  # type: ignore[arg-type]
    assert controller.seat == 1 and room.players[0] == controller.token
    await room.claim_seat(spectator, spectator_socket, 1)  # type: ignore[arg-type]
    assert spectator.seat is None and room.players[0] == controller.token

    await room.command(controller, controller_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "running"
    await room.disconnect(controller, controller_socket)  # type: ignore[arg-type]
    assert controller.seat == 1 and controller.disconnect_task is not None
    reconnect_socket = TestSocket()
    await room.join(reconnect_socket, "Ada", controller.token, 3)  # type: ignore[arg-type]
    assert controller.socket is reconnect_socket and controller.seat == 1 and room.phase == "running"
    assert controller.disconnect_task is None
    assert "è rientrato" in room.status

    room.disconnect_grace_seconds = 0
    await room.disconnect(controller, reconnect_socket)  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    assert controller.seat is None and room.players[0] is None
    assert not room.started and room.phase == "idle" and room.score == 0
    late_socket = TestSocket()
    await room.join(late_socket, "Ada", controller.token, 1)  # type: ignore[arg-type]
    assert controller.seat is None and room._role(controller)[0] == "spectator"
    await room.command(controller, reconnect_socket, "space")  # type: ignore[arg-type]
    assert room.phase == "idle"
    await room.claim_seat(controller, late_socket, 1)  # type: ignore[arg-type]
    assert controller.seat == 1 and room.players[0] == controller.token

    manual_records_path = Path(temporary_records.name) / "manual-records.json"
    manual = GameRoom(words=["uno"], double_words=["due"], records_path=manual_records_path)
    manual_controller_socket = TestSocket()
    manual_controller = Session("manual-controller", "Ada", seat=1, socket=manual_controller_socket)  # type: ignore[arg-type]
    manual_helper = Session("manual-helper", "Bruno", seat=2)  # type: ignore[arg-type]
    manual_guesser = Session("manual-guesser", "Clara", seat=3)  # type: ignore[arg-type]
    manual.sessions = {session.token: session for session in (manual_controller, manual_helper, manual_guesser)}
    manual.players = [manual_controller.token, manual_helper.token, manual_guesser.token]
    manual.started = True
    manual.correct = 2
    await manual.command(manual_controller, manual_controller_socket, "finish")  # type: ignore[arg-type]
    assert manual.phase == "finished" and manual.records == [("Ada - Bruno - Clara", 2)]
    await manual.close()
    write_records(records_path, [*room.records, ("Zeta", 18), ("Alfa", 18)])
    reloaded = GameRoom(words=["uno"], double_words=["due"], records_path=records_path)
    assert reloaded.records == [("Alfa", 18), ("Zeta", 18), ("Ada - Bruno - Clara", 15)]
    await reloaded.close()
    await room.close()
    temporary_records.cleanup()


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
