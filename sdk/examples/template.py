"""Khung chiến thuật: có sẵn các hàm mô phỏng nước đi. Sửa hàm `evaluate` để đổi lối chơi.

Ý tưởng: với mỗi nước hợp lệ, thử đi nước đó trên bản sao bàn cờ, giả sử đối thủ đáp trả
nước tốt nhất đối với họ, rồi chọn nước cho điểm cao nhất (nhìn trước 2 nửa lượt).
"""
import random
import time
from collections import deque

BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}   # Đấm > Kéo > Lá > Đấm
WALL = {'owner': None, 'type': 'wall'}                                # vật cản: không đi vào, không ăn được
STEPS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
WIN = 10_000
DIST = {}          # DIST[phe] = bản đồ khoảng cách tới ô đích của phe đó; choose_move điền vào mỗi lượt


def other(side):
    return 'p2' if side == 'p1' else 'p1'


def goal_of(board, side):
    """Ô đích của `side`. Bàn 9×9 hay 8×8 đều theo công thức này."""
    size = len(board)
    return (0, size - 1) if side == 'p1' else (size - 1, 0)


def distance_map(size, obstacles, goal):
    """dist[r][c] = số nước ít nhất từ ô (r, c) tới ô đích `goal`, đi 8 hướng, không qua vật cản (bỏ qua các quân)."""
    blocked = {tuple(o) for o in obstacles}
    dist = [[None] * size for _ in range(size)]
    dist[goal[0]][goal[1]] = 0
    queue = deque([tuple(goal)])
    while queue:
        r, c = queue.popleft()
        for dr, dc in STEPS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < size and 0 <= nc < size and (nr, nc) not in blocked and dist[nr][nc] is None:
                dist[nr][nc] = dist[r][c] + 1
                queue.append((nr, nc))
    return dist


def with_walls(observation):
    """Bản sao bàn cờ có đánh dấu vật cản (trong observation['board'] vật cản hiện là None)."""
    board = [row[:] for row in observation['board']]
    for r, c in observation.get('obstacles', []):
        board[r][c] = WALL
    return board


def moves_of(board, side):
    """Mọi nước hợp lệ của `side` trên `board` dưới dạng (hàng, cột, hàng_mới, cột_mới)."""
    result = []
    size = len(board)
    for r in range(size):
        for c in range(size):
            piece = board[r][c]
            if not piece or piece['owner'] != side:
                continue
            for dr, dc in STEPS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < size and 0 <= nc < size:
                    target = board[nr][nc]
                    if target is None or (target['owner'] != side and BEATS[piece['type']] == target['type']):
                        result.append((r, c, nr, nc))
    return result


def after(board, move):
    """Bản sao bàn cờ sau khi đi `move` (không đụng vào bàn cờ gốc)."""
    r, c, nr, nc = move
    copy = [row[:] for row in board]
    copy[nr][nc], copy[r][c] = copy[r][c], None
    return copy


def counts(board, side):
    total = {'rock': 0, 'paper': 0, 'scissors': 0}
    for row in board:
        for piece in row:
            if piece and piece['owner'] == side:
                total[piece['type']] += 1
    return total


def won(board, side, move):
    """Nước `move` có thắng ngay không? (chạm ô đích, hoặc ăn nốt quân cuối của một loại)"""
    if (move[2], move[3]) == goal_of(board, side):
        return True
    return any(n == 0 for n in counts(after(board, move), other(side)).values())


def evaluate(board, side):
    """Điểm của thế cờ đối với `side`: càng cao càng tốt. HÃY SỬA HÀM NÀY."""
    score = 0
    for who, sign in ((side, 1), (other(side), -1)):
        mine = counts(board, who)
        score += sign * 100 * sum(mine.values())                 # nhiều quân là tốt
        score -= sign * 150 * sum(1 for n in mine.values() if n == 1)   # chỉ còn 1 quân của một loại là nguy hiểm
        size = len(board)
        dist = DIST[who]                                          # số nước THẬT tới ô đích (đi vòng vật cản)
        nearest = min((dist[r][c] for r in range(size) for c in range(size)
                       if board[r][c] and board[r][c]['owner'] == who), default=size)
        score -= sign * 10 * nearest                              # quân càng gần ô đích càng tốt
    return score


def choose_move(observation):
    side = observation['mySide']
    board = with_walls(observation)      # bàn 8×8 hay 9×9, có thể có vật cản
    size, walls = observation['size'], observation['obstacles']
    DIST[side] = distance_map(size, walls, observation['goal'])
    DIST[other(side)] = distance_map(size, walls, observation['opponentGoal'])
    started = time.perf_counter()
    best, best_score = None, -10 ** 9
    candidates = moves_of(board, side)
    random.shuffle(candidates)                                    # tránh lặp một nước mãi
    for move in candidates:
        if time.perf_counter() - started > 1.0:                   # luôn chừa thời gian: tối đa 3 giây/lượt
            break
        if won(board, side, move):
            best = move
            break
        reply_board = after(board, move)
        replies = moves_of(reply_board, other(side))
        # Đối thủ thắng ngay ở nước kế tiếp => nước này tệ.
        if any(won(reply_board, other(side), reply) for reply in replies):
            score = -WIN
        else:
            worst = min((evaluate(after(reply_board, reply), side) for reply in replies), default=0)
            score = worst
        if score > best_score:
            best, best_score = move, score
    if best is None:
        move = observation['legalMoves'][0]
        return {'from': move['from'], 'to': move['to']}
    return {'from': [best[0], best[1]], 'to': [best[2], best[3]]}
