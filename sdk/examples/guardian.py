"""Người gác: tránh ô bị đối thủ khắc chế tấn công, bảo vệ loại quân cuối."""
import random


def choose_move(observation):
    board, side = observation['board'], observation['mySide']
    beats = {'rock': 'scissors', 'scissors': 'paper', 'paper': 'rock'}
    goal, size = observation['goal'], observation['size']

    def score(move):
        r, c = move['to']
        piece = board[move['from'][0]][move['from'][1]]
        remaining = sum(p is not None and p['owner'] == side and p['type'] == piece['type']
                        for row in board for p in row)
        threats = 0
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if 0 <= r+dr < size and 0 <= c+dc < size:
                    enemy = board[r+dr][c+dc]
                    if enemy and enemy['owner'] != side and beats[enemy['type']] == piece['type']:
                        threats += 1
        return ((1000 if [r, c] == goal else 0) + 12 * move['capture']
                - threats * (30 if remaining == 1 else 10)
                - max(abs(r-goal[0]), abs(c-goal[1])) + random.random())

    move = max(observation['legalMoves'], key=score)
    return {'from': move['from'], 'to': move['to']}
