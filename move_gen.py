import shogi
import random

def random_move(board) -> str:
    return random.choice(list(board.generate_legal_moves())).usi()