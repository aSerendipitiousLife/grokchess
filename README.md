# grokchess

A friendly arena where home-grown chess engines play **each other** and their
**human authors**. You write a small Python class that picks a move; grokchess
referees the games, runs tournaments, and lets you play any engine in your
browser.

Built for a group of friends learning to write chess engines — start dumb
(random moves), climb the leagues, and try to beat each other.

## How it works (the one big idea)

Engines never talk to each other directly. A **referee** (the *arena*) holds the
one true board and shuttles moves between players:

```
        ┌──────────────── Arena (referee) ─────────────────┐
        │ holds the real board · validates every move ·    │
        │ enforces the time limit · records the game (PGN) │
        └───────────────────────────────────────────────────┘
             ▲                    ▲                    ▲
       Engine (a class      Human (moves from      Tournament
        that picks a         the web board)         (round-robin)
        move)
```

A **human is just another player** whose "pick a move" means "click the board",
so the same referee code runs both engine-vs-engine tournaments and your
human-vs-engine browser games. python-chess knows all the rules, so no engine
has to.

## Quickstart

```bash
git clone https://github.com/grokdatum/grokchess
cd grokchess
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # core + web + test deps

# 1. Run a tournament between the built-in example engines
python -m grokchess.tournament -v

# 2. Play an engine in your browser
python -m grokchess.web          # then open http://127.0.0.1:8000

# Or open it in a standalone browser app window
python -m grokchess.desktop
```

On Windows PowerShell with the local virtual environment:

```powershell
& ".\.venv\Scripts\python.exe" -m grokchess.desktop
```

On Linux/macOS with the local virtual environment:

```bash
./.venv/bin/python -m grokchess.desktop
```

The web app records game and tournament metrics in a local SQLite database at
`data/grokchess.sqlite` by default. Use the Player login field before starting a
game if you want your human moves tracked; tournament simulations automatically
track engine metrics.

## Write your own engine

New to pull requests? [`CONTRIBUTING.md`](CONTRIBUTING.md) walks through the
whole flow step by step.

```bash
cp -r engines/_template engines/<your-name>
$EDITOR engines/<your-name>/engine.py
python -m grokchess.tournament -v
```

The entire contract:

```python
import chess
from grokchess.engine_base import Engine

class MyEngine(Engine):
    name = "my-engine"
    author = "your-name"
    league = "L0"                      # L0 no-lookahead · L1 search≤3 · L2 open

    def choose_move(self, board: chess.Board) -> chess.Move:
        # board is a *copy* — push/pop freely while you think.
        return best_move_you_can_find(board)
```

Return a legal move within the time limit and you're in. See
[`engines/eric/`](engines/eric/) for three worked examples:
`random-mover` → `greedy-material` (one-ply) → `minimax-ab` (alpha-beta search).

## The rules

Short version: **stdlib + `chess` imports only**, **no borrowed brains** (no
Stockfish, no cloud APIs, no opening books/tablebases), **legal move within 1
second**, and you declare a **league**. Full text and the fair-play rationale in
[`RULES.md`](RULES.md).

CI checks every pull request: the import allowlist, the tests, and a smoke
tournament so a broken engine can't merge.

## Layout

| Path | What |
|------|------|
| `grokchess/arena.py` | the referee — plays one game, enforces limits |
| `grokchess/tournament.py` | round-robin + leaderboard (`python -m grokchess.tournament`) |
| `grokchess/web/` | FastAPI backend + self-contained browser board |
| `grokchess/discovery.py` | finds engines under `engines/` |
| `engines/<name>/` | one folder per person |
| `tools/check_imports.py` | the "no borrowed brains" import gate |
| `tests/` | referee + tournament tests |

## Roadmap ideas

- A UCI bridge so engines can be written in any language and play against Stockfish as a benchmark.
- Per-engine process isolation (each engine in its own sandboxed process) — pairs naturally with the UCI bridge; see the accepted-risk note in RULES.md §6.
- GitHub Action that runs the tournament on every merge and publishes a leaderboard.
- Web polish that's genuinely fun to add: a move list + PGN download, league-filtered standings, board themes.

> We deliberately did **not** use [chessground](https://github.com/lichess-org/chessground) (lichess's board): it's GPL-3.0, which would force this MIT repo to GPL, and pulling it from a CDN would break the works-offline-after-clone property. The board is hand-built vanilla JS/SVG instead — a few hundred readable lines you're welcome to hack on.

## Credits

Chess piece art is the **Cburnett** set (the pieces Wikipedia and lichess use), by
Colin M.L. Burnett, used under the BSD 3-Clause license — see
[`grokchess/web/static/pieces/LICENSE`](grokchess/web/static/pieces/LICENSE).

## License

MIT — see [`LICENSE`](LICENSE). (The bundled piece art is BSD-licensed; see Credits.)
