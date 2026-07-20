"""Importable slow engine fixture for web process-worker tests."""

import time

from grokchess.engine_base import Engine


class SlowWebEngine(Engine):
    name = "slow-web-engine"
    author = "test"
    league = "L0"

    def choose_move(self, board):
        time.sleep(0.75)
        return next(iter(board.legal_moves))
