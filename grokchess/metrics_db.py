"""Game and move metrics storage for grokchess.

SQLite is the default local backend. Set ``GROKCHESS_DB_BACKEND=postgres`` and
``GROKCHESS_DATABASE_URL`` to write the same metrics into Supabase/Postgres.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import chess

from .arena import GameResult

DB_PATH = Path(os.environ.get("GROKCHESS_DB_PATH", "data/grokchess.sqlite"))
DATABASE_URL = os.environ.get("GROKCHESS_DATABASE_URL", "")
DB_BACKEND = os.environ.get(
    "GROKCHESS_DB_BACKEND", "postgres" if DATABASE_URL else "sqlite"
).lower()

_LOCK = threading.Lock()
_READY = False


def _is_postgres() -> bool:
    return DB_BACKEND in {"postgres", "postgresql", "supabase"}


def _placeholder() -> str:
    return "%s" if _is_postgres() else "?"


def _connect():
    if _is_postgres():
        if not DATABASE_URL:
            raise RuntimeError("GROKCHESS_DATABASE_URL is required for Postgres metrics")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Postgres metrics require `psycopg`. Install with "
                '`uv sync --extra dev --extra postgres`.'
            ) from exc
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    global _READY
    if _READY:
        return
    with _LOCK, _connect() as conn:
        if _is_postgres():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    white_name TEXT NOT NULL,
                    white_kind TEXT NOT NULL,
                    white_player_id TEXT REFERENCES players(id),
                    black_name TEXT NOT NULL,
                    black_kind TEXT NOT NULL,
                    black_player_id TEXT REFERENCES players(id),
                    result TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    started_at DOUBLE PRECISION NOT NULL,
                    finished_at DOUBLE PRECISION,
                    ply_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moves (
                    id BIGSERIAL PRIMARY KEY,
                    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                    ply INTEGER NOT NULL,
                    actor_name TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    color TEXT NOT NULL,
                    uci TEXT NOT NULL,
                    piece TEXT NOT NULL,
                    captured TEXT,
                    is_capture INTEGER NOT NULL,
                    is_check INTEGER NOT NULL,
                    is_checkmate INTEGER NOT NULL,
                    fen_after TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_moves_actor ON moves(actor_kind, actor_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_games_white ON games(white_kind, white_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_games_black ON games(black_kind, black_name)")
        else:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    white_name TEXT NOT NULL,
                    white_kind TEXT NOT NULL,
                    white_player_id TEXT REFERENCES players(id),
                    black_name TEXT NOT NULL,
                    black_kind TEXT NOT NULL,
                    black_player_id TEXT REFERENCES players(id),
                    result TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    ply_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                    ply INTEGER NOT NULL,
                    actor_name TEXT NOT NULL,
                    actor_kind TEXT NOT NULL,
                    color TEXT NOT NULL,
                    uci TEXT NOT NULL,
                    piece TEXT NOT NULL,
                    captured TEXT,
                    is_capture INTEGER NOT NULL,
                    is_check INTEGER NOT NULL,
                    is_checkmate INTEGER NOT NULL,
                    fen_after TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_moves_actor ON moves(actor_kind, actor_name);
                CREATE INDEX IF NOT EXISTS idx_games_white ON games(white_kind, white_name);
                CREATE INDEX IF NOT EXISTS idx_games_black ON games(black_kind, black_name);
                """
            )
        _READY = True


def _fetchone(conn, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _executemany(conn, sql: str, rows: list[tuple]) -> None:
    if not rows:
        return
    if hasattr(conn, "executemany"):
        conn.executemany(sql, rows)
        return
    with conn.cursor() as cursor:
        cursor.executemany(sql, rows)


def _insert_game(
    conn,
    *,
    game_id: str,
    mode: str,
    white_name: str,
    white_kind: str,
    black_name: str,
    black_kind: str,
    started_at: float,
    white_player_id: str | None = None,
    black_player_id: str | None = None,
) -> None:
    ph = _placeholder()
    conn.execute(
        f"""
        INSERT INTO games (
            id, mode, white_name, white_kind, white_player_id,
            black_name, black_kind, black_player_id, started_at
        )
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """,
        (
            game_id,
            mode,
            white_name,
            white_kind,
            white_player_id,
            black_name,
            black_kind,
            black_player_id,
            started_at,
        ),
    )


def _move_event_and_row(
    game_id: str,
    board_before: chess.Board,
    move: chess.Move,
    *,
    actor_name: str,
    actor_kind: str,
) -> tuple[dict, tuple]:
    moving_piece = board_before.piece_at(move.from_square)
    captured_piece = board_before.piece_at(move.to_square)
    if board_before.is_en_passant(move):
        offset = -8 if board_before.turn == chess.WHITE else 8
        captured_piece = board_before.piece_at(move.to_square + offset)
    board_after = board_before.copy()
    board_after.push(move)
    event = {
        "uci": move.uci(),
        "from": chess.square_name(move.from_square),
        "to": chess.square_name(move.to_square),
        "piece": moving_piece.symbol() if moving_piece else "",
        "captured": captured_piece.symbol() if captured_piece else None,
        "actor": actor_name,
    }
    return event, (
        game_id,
        board_before.ply() + 1,
        actor_name,
        actor_kind,
        "white" if board_before.turn == chess.WHITE else "black",
        move.uci(),
        event["piece"],
        event["captured"],
        int(event["captured"] is not None),
        int(board_after.is_check()),
        int(board_after.is_checkmate()),
        board_after.fen(),
    )


def _insert_move_rows(conn, rows: list[tuple]) -> None:
    ph = _placeholder()
    _executemany(
        conn,
        f"""
        INSERT INTO moves (
            game_id, ply, actor_name, actor_kind, color, uci, piece, captured,
            is_capture, is_check, is_checkmate, fen_after
        )
        VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """,
        rows,
    )


def _finish_game_row(
    conn,
    game_id: str,
    result: str,
    reason: str,
    detail: str = "",
    *,
    finished_at: float | None = None,
    ply_count: int | None = None,
) -> None:
    ph = _placeholder()
    if ply_count is None:
        conn.execute(
            f"""
            UPDATE games
            SET result = {ph}, reason = {ph}, detail = {ph}, finished_at = COALESCE(finished_at, {ph})
            WHERE id = {ph}
            """,
            (result, reason, detail, finished_at or time.time(), game_id),
        )
        return
    conn.execute(
        f"""
        UPDATE games
        SET result = {ph}, reason = {ph}, detail = {ph},
            finished_at = COALESCE(finished_at, {ph}), ply_count = {ph}
        WHERE id = {ph}
        """,
        (result, reason, detail, finished_at or time.time(), ply_count, game_id),
    )


def login_player(name: str) -> dict:
    init_db()
    clean = " ".join(name.strip().split())
    if not clean:
        raise ValueError("player name is required")
    ph = _placeholder()
    with _LOCK, _connect() as conn:
        row = _fetchone(conn, f"SELECT id, name FROM players WHERE lower(name) = lower({ph})", (clean,))
        if row is None:
            player_id = uuid.uuid4().hex[:12]
            conn.execute(
                f"INSERT INTO players (id, name, created_at) VALUES ({ph}, {ph}, {ph})",
                (player_id, clean, time.time()),
            )
            row = _fetchone(conn, f"SELECT id, name FROM players WHERE id = {ph}", (player_id,))
        return row


def start_game(
    *,
    mode: str,
    white_name: str,
    white_kind: str,
    black_name: str,
    black_kind: str,
    white_player_id: str | None = None,
    black_player_id: str | None = None,
) -> str:
    init_db()
    game_id = uuid.uuid4().hex[:12]
    with _LOCK, _connect() as conn:
        _insert_game(
            conn,
            game_id=game_id,
            mode=mode,
            white_name=white_name,
            white_kind=white_kind,
            white_player_id=white_player_id,
            black_name=black_name,
            black_kind=black_kind,
            black_player_id=black_player_id,
            started_at=time.time(),
        )
    return game_id


def record_move(
    game_id: str,
    board_before: chess.Board,
    move: chess.Move,
    *,
    actor_name: str,
    actor_kind: str,
) -> dict:
    init_db()
    event, row = _move_event_and_row(
        game_id,
        board_before,
        move,
        actor_name=actor_name,
        actor_kind=actor_kind,
    )
    ph = _placeholder()
    with _LOCK, _connect() as conn:
        _insert_move_rows(conn, [row])
        conn.execute(f"UPDATE games SET ply_count = ply_count + 1 WHERE id = {ph}", (game_id,))
    return event


def finish_game(game_id: str, result: str, reason: str, detail: str = "") -> None:
    init_db()
    with _LOCK, _connect() as conn:
        _finish_game_row(conn, game_id, result, reason, detail)


def record_result_game(result: GameResult, *, mode: str = "tournament") -> str:
    init_db()
    board = chess.Board()
    game_id = uuid.uuid4().hex[:12]
    move_rows = []
    for uci in result.moves:
        move = chess.Move.from_uci(uci)
        actor_name = result.white if board.turn == chess.WHITE else result.black
        _, row = _move_event_and_row(
            game_id,
            board,
            move,
            actor_name=actor_name,
            actor_kind="engine",
        )
        move_rows.append(row)
        board.push(move)
    now = time.time()
    with _LOCK, _connect() as conn:
        _insert_game(
            conn,
            game_id=game_id,
            mode=mode,
            white_name=result.white,
            white_kind="engine",
            black_name=result.black,
            black_kind="engine",
            started_at=now,
        )
        _insert_move_rows(conn, move_rows)
        _finish_game_row(
            conn,
            game_id,
            result.result,
            result.reason,
            result.detail,
            finished_at=time.time(),
            ply_count=len(move_rows),
        )
    return game_id


def metrics_summary() -> dict:
    init_db()
    with _LOCK, _connect() as conn:
        games = [
            dict(row)
            for row in conn.execute(
                """
                WITH participants AS (
                    SELECT id, white_name AS name, white_kind AS kind, result, ply_count,
                           CASE WHEN result = '1-0' THEN 1 ELSE 0 END AS win,
                           CASE WHEN result = '0-1' THEN 1 ELSE 0 END AS loss,
                           CASE WHEN result = '1/2-1/2' THEN 1 ELSE 0 END AS draw
                    FROM games WHERE result != ''
                    UNION ALL
                    SELECT id, black_name AS name, black_kind AS kind, result, ply_count,
                           CASE WHEN result = '0-1' THEN 1 ELSE 0 END AS win,
                           CASE WHEN result = '1-0' THEN 1 ELSE 0 END AS loss,
                           CASE WHEN result = '1/2-1/2' THEN 1 ELSE 0 END AS draw
                    FROM games WHERE result != ''
                )
                SELECT kind, name, COUNT(*) AS games, SUM(win) AS wins, SUM(loss) AS losses,
                       SUM(draw) AS draws, ROUND(AVG(ply_count), 1) AS avg_plies
                FROM participants
                GROUP BY kind, name
                """
            ).fetchall()
        ]
        moves = [
            dict(row)
            for row in conn.execute(
                """
                SELECT actor_kind AS kind, actor_name AS name, COUNT(*) AS moves,
                       SUM(is_capture) AS captures, SUM(is_check) AS checks,
                       SUM(is_checkmate) AS checkmates
                FROM moves
                GROUP BY actor_kind, actor_name
                """
            ).fetchall()
        ]

    by_key = {}
    for item in games:
        item.update({"moves": 0, "captures": 0, "checks": 0, "checkmates": 0})
        by_key[(item["kind"], item["name"])] = item
    for row in moves:
        key = (row["kind"], row["name"])
        item = by_key.setdefault(
            key,
            {
                "kind": row["kind"],
                "name": row["name"],
                "games": 0,
                "wins": 0,
                "losses": 0,
                "draws": 0,
                "avg_plies": 0,
            },
        )
        item.update(
            {
                "moves": row["moves"] or 0,
                "captures": row["captures"] or 0,
                "checks": row["checks"] or 0,
                "checkmates": row["checkmates"] or 0,
            }
        )

    items = sorted(by_key.values(), key=lambda item: (item["kind"], item["name"].lower()))
    return {
        "players": [item for item in items if item["kind"] == "player"],
        "engines": [item for item in items if item["kind"] == "engine"],
    }
