"""FastAPI backend for playing a human against an engine.

The human is just another player: the referee logic (validate the move, let the
engine reply, decide the result) is the same one the tournament uses. python-chess
is the source of truth for legality, so a tampered browser can't cheat.

Games are held in memory, so this is a single-process, single-user-ish server —
perfect for "clone it and play on your laptop", not a public multi-user site.

Run it with:  python -m grokchess.web   (from the repo root)
"""

from __future__ import annotations

import itertools
import os
import threading
import uuid
from pathlib import Path

import chess
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..arena import DEFAULT_TIME_LIMIT, MoveTimeout, _natural_reason, call_with_time_limit, play_game
from ..discovery import load_engines
from ..metrics_db import (
    finish_game,
    login_player,
    metrics_summary,
    record_move,
    record_result_game,
    start_game,
)
from ..tournament import Standing, _tally

TIME_LIMIT = float(os.environ.get("GROKCHESS_TIME_LIMIT", DEFAULT_TIME_LIMIT))
ENGINES_DIR = os.environ.get("GROKCHESS_ENGINES_DIR", "engines")
MAX_GAMES = 50  # in-memory game cap; oldest games are evicted past this
MAX_TOURNAMENTS = 10

app = FastAPI(title="grokchess")

# Serve the vendored piece art (and any future assets) locally — no CDN, so the
# board works fully offline.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

_INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")

# name -> engine class
_REGISTRY: dict[str, type] = {}
# game_id -> game state
_GAMES: dict[str, dict] = {}
# tournament_id -> job state
_TOURNAMENTS: dict[str, dict] = {}
_TOURNAMENT_LOCK = threading.Lock()


def _database_detail(exc: Exception) -> str:
    detail = f"Database unavailable: {exc!r}"
    if "supabase.co" in str(exc) and "failed to resolve host 'db." in str(exc):
        detail += (
            ". Supabase direct database hosts can be IPv6-only; use the "
            "Session pooler or Transaction pooler connection string instead."
        )
    return detail


def _raise_database_error(exc: Exception) -> None:
    raise HTTPException(status_code=503, detail=_database_detail(exc)) from exc


def _add_db_warning(game: dict, exc: Exception) -> None:
    warning = _database_detail(exc)
    lock = game.get("lock")
    if lock:
        with lock:
            warnings = game.setdefault("db_warnings", [])
            if warning not in warnings:
                warnings.append(warning)
        return
    warnings = game.setdefault("db_warnings", [])
    if warning not in warnings:
        warnings.append(warning)


def _run_db_later(game: dict, work) -> None:
    def runner() -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - gameplay should not wait on metrics
            _add_db_warning(game, exc)

    threading.Thread(target=runner, name="grokchess-metrics", daemon=True).start()


def _queue_engine_reply(game: dict) -> None:
    board = game["board"]
    if (
        game.get("engine_pending")
        or game.get("forfeit")
        or board.is_game_over(claim_draw=True)
        or board.turn == game["human_color"]
    ):
        return
    game["engine_pending"] = True

    def runner() -> None:
        with game["lock"]:
            try:
                _engine_reply(game)
            finally:
                game["engine_pending"] = False

    # Give the browser a chance to paint the human move before CPU-heavy engines think.
    timer = threading.Timer(0.05, runner)
    timer.name = "grokchess-engine"
    timer.daemon = True
    timer.start()


def _registry() -> dict[str, type]:
    if not _REGISTRY:
        for cls in load_engines(ENGINES_DIR):
            _REGISTRY[cls.name] = cls
    return _REGISTRY


class NewGame(BaseModel):
    engine: str
    human_color: str = "white"
    player_id: str | None = None
    player_name: str | None = None


class LoginReq(BaseModel):
    name: str


class TournamentReq(BaseModel):
    rounds: int = Field(default=1, ge=1, le=5)
    max_plies: int = Field(default=80, ge=4, le=400)
    time_limit: float = Field(default=TIME_LIMIT, gt=0, le=5)


class MoveReq(BaseModel):
    game_id: str
    from_sq: str = Field(alias="from")
    to_sq: str = Field(alias="to")
    promotion: str | None = None

    model_config = {"populate_by_name": True}


def _human_wins_status(game: dict) -> str:
    return "white_wins" if game["human_color"] == chess.WHITE else "black_wins"


def _game_result_for_status(status: str) -> str:
    if status == "white_wins":
        return "1-0"
    if status == "black_wins":
        return "0-1"
    if status == "draw":
        return "1/2-1/2"
    return ""


def _status(game: dict):
    """Return (status, reason, detail)."""
    if game.get("forfeit"):
        return game["forfeit"]
    board = game["board"]
    if board.is_game_over(claim_draw=True):
        mapping = {"1-0": "white_wins", "0-1": "black_wins", "1/2-1/2": "draw"}
        return mapping[board.result(claim_draw=True)], _natural_reason(board), ""
    return "playing", "", ""


def _resolve_move(board: chess.Board, frm: str, to: str, promotion: str | None):
    frm, to = frm.lower(), to.lower()
    candidates = [
        m
        for m in board.legal_moves
        if chess.square_name(m.from_square) == frm
        and chess.square_name(m.to_square) == to
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if promotion:
        for m in candidates:
            if m.promotion and chess.piece_symbol(m.promotion) == promotion.lower():
                return m
    for m in candidates:  # default to a queen
        if m.promotion == chess.QUEEN:
            return m
    return candidates[0]


def _push_recorded_move(game: dict, move: chess.Move, actor: str) -> None:
    board = game["board"]
    event = _frame_event(board, move)
    if game.get("db_game_id"):
        board_before = board.copy()
        db_game_id = game["db_game_id"]
        actor_name = game["player_name"] if actor == "human" else game["engine_name"]
        actor_kind = "player" if actor == "human" else "engine"

        def record() -> None:
            record_move(
                db_game_id,
                board_before,
                move,
                actor_name=actor_name,
                actor_kind=actor_kind,
            )

        _run_db_later(game, record)
    event["actor"] = actor
    board.push(move)
    game["last_move"] = move.uci()
    game["move_events"].append(event)
    _finish_db_game_if_needed(game)


def _finish_db_game_if_needed(game: dict) -> None:
    if game.get("db_finished") or not game.get("db_game_id"):
        return
    status, reason, detail = _status(game)
    result = _game_result_for_status(status)
    if result:
        db_game_id = game["db_game_id"]

        def finish() -> None:
            finish_game(db_game_id, result, reason, detail)

        _run_db_later(game, finish)
        game["db_finished"] = True


def _engine_reply(game: dict) -> None:
    """If it's the engine's turn, let it move (or forfeit on error/timeout)."""
    board = game["board"]
    if game.get("forfeit") or board.is_game_over(claim_draw=True):
        return
    if board.turn == game["human_color"]:
        return
    engine = game["engine"]
    try:
        move, _ = call_with_time_limit(
            lambda: engine.choose_move(board.copy()), TIME_LIMIT
        )
    except MoveTimeout as exc:
        game["forfeit"] = (_human_wins_status(game), "timeout", str(exc))
        _finish_db_game_if_needed(game)
        return
    except Exception as exc:  # noqa: BLE001 - engine bug forfeits
        game["forfeit"] = (_human_wins_status(game), "engine_error", repr(exc))
        _finish_db_game_if_needed(game)
        return
    if not isinstance(move, chess.Move) or move not in board.legal_moves:
        game["forfeit"] = (_human_wins_status(game), "illegal_move", str(move))
        _finish_db_game_if_needed(game)
        return
    _push_recorded_move(game, move, "engine")


def _state(game_id: str) -> dict:
    game = _GAMES[game_id]
    with game["lock"]:
        board = game["board"]
        status, reason, detail = _status(game)
        legal = [
            {
                "from": chess.square_name(m.from_square),
                "to": chess.square_name(m.to_square),
                "uci": m.uci(),
                "promotion": chess.piece_symbol(m.promotion) if m.promotion else None,
            }
            for m in board.legal_moves
        ]
        return {
            "game_id": game_id,
            "fen": board.fen(),
            "turn": "white" if board.turn == chess.WHITE else "black",
            "human_color": "white" if game["human_color"] == chess.WHITE else "black",
            "engine": game["engine_name"],
            "legal_moves": legal,
            "last_move": game.get("last_move"),
            "move_events": list(game.get("move_events", [])),
            "check": board.is_check(),
            "status": status,
            "reason": reason,
            "detail": detail,
            "move_number": board.fullmove_number,
            "engine_pending": bool(game.get("engine_pending")),
            "warnings": list(game.get("db_warnings", [])),
        }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


@app.post("/api/login")
def login(req: LoginReq):
    try:
        return login_player(req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface database configuration issues cleanly
        _raise_database_error(exc)


@app.get("/api/metrics")
def metrics():
    try:
        return metrics_summary()
    except Exception as exc:  # noqa: BLE001 - surface database configuration issues cleanly
        _raise_database_error(exc)


@app.get("/api/engines")
def engines():
    return [
        {"name": cls.name, "author": getattr(cls, "author", ""), "league": getattr(cls, "league", "")}
        for cls in _registry().values()
    ]


@app.post("/api/new")
def new_game(req: NewGame):
    registry = _registry()
    if req.engine not in registry:
        raise HTTPException(status_code=404, detail=f"unknown engine: {req.engine}")
    if req.human_color not in ("white", "black"):
        raise HTTPException(status_code=400, detail="human_color must be 'white' or 'black'")
    # Bound the store: dicts iterate in insertion order, so the first key is
    # the oldest game. Without this, every "New game" click leaks memory.
    while len(_GAMES) >= MAX_GAMES:
        _GAMES.pop(next(iter(_GAMES)))
    game_id = uuid.uuid4().hex[:12]
    player = None
    db_warnings = []
    if req.player_id and req.player_name:
        player = {"id": req.player_id, "name": req.player_name}
    elif req.player_name:
        try:
            player = login_player(req.player_name)
        except Exception as exc:  # noqa: BLE001 - surface database configuration issues cleanly
            clean = " ".join(req.player_name.strip().split())
            player = {"id": None, "name": clean or "Human"}
            db_warnings.append(_database_detail(exc))
    player_name = player["name"] if player else "Human"
    player_id = player["id"] if player else None
    human_is_white = req.human_color == "white"
    try:
        db_game_id = start_game(
            mode="human",
            white_name=player_name if human_is_white else req.engine,
            white_kind="player" if human_is_white else "engine",
            white_player_id=player_id if human_is_white else None,
            black_name=req.engine if human_is_white else player_name,
            black_kind="engine" if human_is_white else "player",
            black_player_id=None if human_is_white else player_id,
        )
    except Exception as exc:  # noqa: BLE001 - surface database configuration issues cleanly
        db_game_id = None
        warning = _database_detail(exc)
        if warning not in db_warnings:
            db_warnings.append(warning)
    game = {
        "board": chess.Board(),
        "engine": registry[req.engine](),
        "engine_name": req.engine,
        "human_color": chess.WHITE if req.human_color == "white" else chess.BLACK,
        "player_name": player_name,
        "player_id": player_id,
        "db_game_id": db_game_id,
        "db_finished": False,
        "db_warnings": db_warnings,
        "engine_pending": False,
        "lock": threading.RLock(),
        "last_move": None,
        "move_events": [],
    }
    _GAMES[game_id] = game
    with game["lock"]:
        _queue_engine_reply(game)  # engine moves first if the human chose Black
    return _state(game_id)


@app.post("/api/move")
def move(req: MoveReq):
    if req.game_id not in _GAMES:
        raise HTTPException(status_code=404, detail="unknown game_id")
    game = _GAMES[req.game_id]
    with game["lock"]:
        status, _, _ = _status(game)
        if status != "playing":
            return _state(req.game_id)
        if game.get("engine_pending"):
            raise HTTPException(status_code=400, detail="engine is thinking")
        board = game["board"]
        if board.turn != game["human_color"]:
            raise HTTPException(status_code=400, detail="not your turn")
        chosen = _resolve_move(board, req.from_sq, req.to_sq, req.promotion)
        if chosen is None:
            raise HTTPException(status_code=400, detail=f"illegal move: {req.from_sq}{req.to_sq}")
        _push_recorded_move(game, chosen, "human")
        _finish_db_game_if_needed(game)
        _queue_engine_reply(game)
        return _state(req.game_id)


@app.get("/api/state/{game_id}")
def state(game_id: str):
    if game_id not in _GAMES:
        raise HTTPException(status_code=404, detail="unknown game_id")
    return _state(game_id)


def _frame_event(board: chess.Board, move: chess.Move) -> dict:
    moving_piece = board.piece_at(move.from_square)
    captured_piece = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        offset = -8 if board.turn == chess.WHITE else 8
        captured_piece = board.piece_at(move.to_square + offset)
    return {
        "uci": move.uci(),
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "piece": moving_piece.symbol() if moving_piece else "",
        "captured": captured_piece.symbol() if captured_piece else None,
        "actor": "white" if board.turn == chess.WHITE else "black",
    }


def _game_frames(result) -> list[dict]:
    board = chess.Board()
    frames = [{"fen": board.fen(), "event": None}]
    for uci in result.moves:
        move = chess.Move.from_uci(uci)
        event = _frame_event(board, move)
        board.push(move)
        frames.append({"fen": board.fen(), "event": event})
    return frames


def _standings_payload(standings) -> list[dict]:
    table = sorted(
        standings.values(), key=lambda standing: (standing.points, standing.wins), reverse=True
    )
    return [
        {
            "rank": rank,
            "name": standing.name,
            "author": standing.author,
            "league": standing.league,
            "played": standing.played,
            "wins": standing.wins,
            "draws": standing.draws,
            "losses": standing.losses,
            "points": standing.points,
        }
        for rank, standing in enumerate(table, start=1)
    ]


def _result_payload(result) -> dict:
    return {
        "white": result.white,
        "black": result.black,
        "result": result.result,
        "reason": result.reason,
        "detail": result.detail,
        "moves": result.moves,
        "frames": _game_frames(result),
    }


def _new_standings(classes):
    return {
        cls: Standing(cls.name, getattr(cls, "author", ""), getattr(cls, "league", ""))
        for cls in classes
    }


def _run_tournament_sync(classes, req: TournamentReq, on_game=None) -> dict:
    standings = _new_standings(classes)
    games = []
    warnings = []
    for _ in range(req.rounds):
        for white_cls, black_cls in itertools.permutations(classes, 2):
            result = play_game(
                white_cls(),
                black_cls(),
                time_limit=req.time_limit,
                max_plies=req.max_plies,
            )
            try:
                record_result_game(result, mode="tournament")
            except Exception as exc:  # noqa: BLE001 - metrics should not kill simulations
                warning = _database_detail(exc)
                if warning not in warnings:
                    warnings.append(warning)
            _tally(standings[white_cls], standings[black_cls], result)
            payload = _result_payload(result)
            games.append(payload)
            if on_game is not None:
                on_game(_standings_payload(standings), payload, warnings)
    return {"standings": _standings_payload(standings), "games": games, "warnings": warnings}


def _job_snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "completed_games": job["completed_games"],
        "total_games": job["total_games"],
        "standings": list(job["standings"]),
        "games": list(job["games"]),
        "error": job.get("error"),
        "warnings": list(job.get("warnings", [])),
    }


def _run_tournament_job(job_id: str, classes, req: TournamentReq) -> None:
    def on_game(standings, game, warnings):
        with _TOURNAMENT_LOCK:
            job = _TOURNAMENTS[job_id]
            job["completed_games"] += 1
            job["standings"] = standings
            job["games"].append(game)
            job["warnings"] = warnings

    try:
        result = _run_tournament_sync(classes, req, on_game=on_game)
        with _TOURNAMENT_LOCK:
            job = _TOURNAMENTS[job_id]
            job["status"] = "done"
            job["standings"] = result["standings"]
            job["games"] = result["games"]
            job["warnings"] = result["warnings"]
            job["completed_games"] = job["total_games"]
    except Exception as exc:  # noqa: BLE001 - surfaced to the browser
        with _TOURNAMENT_LOCK:
            job = _TOURNAMENTS[job_id]
            job["status"] = "error"
            job["error"] = repr(exc)


@app.post("/api/tournament/run")
def run_tournament(req: TournamentReq):
    classes = list(_registry().values())
    if len(classes) < 2:
        raise HTTPException(status_code=400, detail="need at least two engines")

    return _run_tournament_sync(classes, req)


@app.post("/api/tournament/start")
def start_tournament(req: TournamentReq):
    classes = list(_registry().values())
    if len(classes) < 2:
        raise HTTPException(status_code=400, detail="need at least two engines")

    with _TOURNAMENT_LOCK:
        while len(_TOURNAMENTS) >= MAX_TOURNAMENTS:
            _TOURNAMENTS.pop(next(iter(_TOURNAMENTS)))
        job_id = uuid.uuid4().hex[:12]
        total_games = req.rounds * len(classes) * (len(classes) - 1)
        _TOURNAMENTS[job_id] = {
            "id": job_id,
            "status": "running",
            "completed_games": 0,
            "total_games": total_games,
            "standings": _standings_payload(_new_standings(classes)),
            "games": [],
            "error": None,
            "warnings": [],
        }

    worker = threading.Thread(
        target=_run_tournament_job,
        args=(job_id, classes, req),
        name="grokchess-tournament",
        daemon=True,
    )
    worker.start()
    with _TOURNAMENT_LOCK:
        return _job_snapshot(_TOURNAMENTS[job_id])


@app.get("/api/tournament/status/{job_id}")
def tournament_status(job_id: str):
    with _TOURNAMENT_LOCK:
        if job_id not in _TOURNAMENTS:
            raise HTTPException(status_code=404, detail="unknown tournament")
        return _job_snapshot(_TOURNAMENTS[job_id])
