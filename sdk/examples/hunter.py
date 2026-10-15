"""Thợ săn: ưu tiên đích, sau đó ăn quân, sau đó tiến về góc đích."""
import random


def choose_move(observation):
    goal = [0, 8] if observation['mySide'] == 'p1' else [8, 0]

    def score(move):
        distance = max(abs(move['to'][0] - goal[0]), abs(move['to'][1] - goal[1]))
        return (1000 if move['to'] == goal else 0) + 20 * move['capture'] - distance + random.random()

    move = max(observation['legalMoves'], key=score)
    return {'from': move['from'], 'to': move['to']}
