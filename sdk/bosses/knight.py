"""Boss 4 · Kỵ sĩ: nhìn trước hai nửa lượt (nước của mình và nước đáp trả tốt nhất)."""
import random
import time

BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}
PREDATOR = {prey: hunter for hunter, prey in BEATS.items()}
GOALS = {'p1': (0, 8), 'p2': (8, 0)}
STEPS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
WIN = 1_000_000
MAX_DEPTH = 2
QUIESCENCE = 0


class Timeout(Exception):
    pass


def other(side):
    return 'p2' if side == 'p1' else 'p1'


class Position:
    def __init__(self, board):
        self.cells = [[(p['owner'], p['type']) if p else None for p in row] for row in board]
        self.count = {s: {t: 0 for t in BEATS} for s in GOALS}
        for row in self.cells:
            for piece in row:
                if piece:
                    self.count[piece[0]][piece[1]] += 1

    def moves(self, side):
        out, cells = [], self.cells
        gr, gc = GOALS[side]
        for r in range(9):
            for c in range(9):
                piece = cells[r][c]
                if piece is None or piece[0] != side:
                    continue
                prey = BEATS[piece[1]]
                for dr, dc in STEPS:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < 9 and 0 <= nc < 9:
                        target = cells[nr][nc]
                        if target is None:
                            out.append((r, c, nr, nc, None))
                        elif target[0] != side and target[1] == prey:
                            out.append((r, c, nr, nc, target))
        # Captures and steps towards the goal first: alpha-beta cuts far more.
        out.sort(key=lambda m: (m[4] is None, max(abs(m[2] - gr), abs(m[3] - gc))))
        return out

    def play(self, move, side):
        r, c, nr, nc, taken = move
        self.cells[nr][nc], self.cells[r][c] = self.cells[r][c], None
        if taken:
            self.count[taken[0]][taken[1]] -= 1
            if self.count[taken[0]][taken[1]] == 0:
                return True
        return (nr, nc) == GOALS[side]

    def undo(self, move):
        r, c, nr, nc, taken = move
        self.cells[r][c], self.cells[nr][nc] = self.cells[nr][nc], taken
        if taken:
            self.count[taken[0]][taken[1]] += 1

    def key(self, turn):
        return turn + ':' + '/'.join(''.join(p[0] + p[1][0] if p else '.' for p in row) for row in self.cells)


def evaluate(pos, side):
    """Score for the side to move: material, fragile types, race to the goal, hanging pieces."""
    cells, score = pos.cells, 0
    nearest = {'p1': 9, 'p2': 9}
    advance = {'p1': 0, 'p2': 0}
    hanging = {'p1': 0, 'p2': 0}
    for r in range(9):
        for c in range(9):
            piece = cells[r][c]
            if not piece:
                continue
            who, kind = piece
            gr, gc = GOALS[who]
            distance = max(abs(r - gr), abs(c - gc))
            nearest[who] = min(nearest[who], distance)
            advance[who] += 8 - distance
            hunter = PREDATOR[kind]
            for dr, dc in STEPS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < 9 and 0 <= nc < 9:
                    enemy = cells[nr][nc]
                    if enemy and enemy[0] != who and enemy[1] == hunter:
                        hanging[who] += 120 if pos.count[who][kind] == 1 else 30
                        break
    for who, sign in ((side, 1), (other(side), -1)):
        counts = pos.count[who]
        score += sign * (100 * sum(counts.values()) - 110 * sum(n == 1 for n in counts.values())
                         - 18 * nearest[who] + 4 * advance[who] - hanging[who])
    # The side to move gets to act on threats first.
    return score + hanging[other(side)] // 2


class Search:
    def __init__(self, pos, deadline):
        self.pos, self.deadline, self.nodes = pos, deadline, 0

    def negamax(self, side, depth, alpha, beta):
        self.nodes += 1
        if self.nodes & 255 == 0 and time.perf_counter() > self.deadline:
            raise Timeout
        if depth <= 0:
            return self.quiesce(side, alpha, beta, QUIESCENCE)
        moves = self.pos.moves(side)
        if not moves:
            return 0
        best = -WIN * 2
        for move in moves:
            if self.pos.play(move, side):
                value = WIN + depth
            else:
                value = -self.negamax(other(side), depth - 1, -beta, -alpha)
            self.pos.undo(move)
            if value > best:
                best = value
                if value > alpha:
                    alpha = value
                    if alpha >= beta:
                        break
        return best

    def quiesce(self, side, alpha, beta, left):
        """Play out captures at the horizon so the score is not taken mid-exchange."""
        self.nodes += 1
        if self.nodes & 255 == 0 and time.perf_counter() > self.deadline:
            raise Timeout
        stand = evaluate(self.pos, side)
        if left <= 0 or stand >= beta:
            return stand
        alpha = max(alpha, stand)
        for move in self.pos.moves(side):
            if move[4] is None:
                break
            if self.pos.play(move, side):
                value = WIN
            else:
                value = -self.quiesce(other(side), -beta, -alpha, left - 1)
            self.pos.undo(move)
            if value > alpha:
                alpha = value
                if alpha >= beta:
                    break
        return alpha


def choose_move(observation):
    side = observation['mySide']
    rng = random.Random(observation.get('seed', 0))
    budget = min(0.6, max(0.03, observation.get('timeLeft', 60) / 70))
    deadline = time.perf_counter() + budget
    pos = Position(observation['board'])
    repetitions = observation.get('repetitions') or {}
    legal = {(m['from'][0], m['from'][1], m['to'][0], m['to'][1]) for m in observation['legalMoves']}
    root = [m for m in pos.moves(side) if m[:4] in legal] or \
        [(r, c, nr, nc, pos.cells[nr][nc]) for r, c, nr, nc in legal]
    jitter = {move: rng.random() for move in root}
    best, search = root[0], Search(pos, deadline)
    for depth in range(1, MAX_DEPTH + 1):
        scored = []
        try:
            alpha = -WIN * 3
            for move in root:
                if pos.play(move, side):
                    value = WIN + depth
                else:
                    value = -search.negamax(other(side), depth - 1, -WIN * 3, -alpha)
                    # A third repetition is a draw: avoid it while ahead, welcome it while behind.
                    if repetitions.get(pos.key(other(side)), 0) >= 2:
                        value = 0
                pos.undo(move)
                scored.append((value + jitter[move], move))
                alpha = max(alpha, value)
                if value >= WIN:
                    break
        except Timeout:
            # Inner frames left moves on the board; this position is discarded anyway.
            winning = [item for item in scored if item[0] >= WIN]
            if winning:
                best = winning[0][1]
            break
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0][1]
        if scored[0][0] >= WIN:
            break
        root = [move for _, move in scored] + [m for m in root if m not in {s[1] for s in scored}]
    return {'from': [best[0], best[1]], 'to': [best[2], best[3]]}
