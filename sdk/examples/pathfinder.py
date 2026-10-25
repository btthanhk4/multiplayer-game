"""Người dẫn đường: chạy tốt trên MỌI bản đồ (bàn 8×8 hay 9×9, có hoặc không có vật cản).

Điểm khác với bot Thợ săn: "còn bao xa tới đích" được tính bằng đường đi thật (tìm đường theo chiều rộng, đi vòng
vật cản) chứ không phải khoảng cách thẳng. Trên bản đồ Dòng sông, quân đứng sát bức tường chưa chắc đã gần đích:
phải đi vòng qua cầu. Mỗi nước được chấm theo thứ tự ưu tiên: chạm đích > ăn quân > tránh bị khắc chế > tiến gần đích.
"""
import random
from collections import deque

STEPS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
PREDATOR = {'scissors': 'rock', 'paper': 'scissors', 'rock': 'paper'}      # loại ăn được quân này


def distance_map(size, obstacles, goal):
    """dist[r][c] = số nước ít nhất từ ô (r, c) tới `goal`, đi 8 hướng, không đi qua vật cản (bỏ qua các quân)."""
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


def choose_move(observation):
    board, side = observation['board'], observation['mySide']
    size, goal = observation['size'], observation['goal']
    dist = distance_map(size, observation['obstacles'], goal)

    def threats(r, c, piece):
        """Số quân đối phương đứng cạnh ô (r, c) và khắc chế được quân `piece` đứng ở đó."""
        count = 0
        for dr, dc in STEPS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < size and 0 <= nc < size:
                enemy = board[nr][nc]
                if enemy and enemy['owner'] != side and enemy['type'] == PREDATOR[piece['type']]:
                    count += 1
        return count

    def score(move):
        (fr, fc), (r, c) = move['from'], move['to']
        piece = board[fr][fc]
        remaining = sum(1 for row in board for p in row if p and p['owner'] == side and p['type'] == piece['type'])
        value = 1000 if [r, c] == goal else 0
        value += 12 * move['capture']
        value -= threats(r, c, piece) * (30 if remaining == 1 else 10)     # đừng bước vào chỗ bị khắc chế
        value -= dist[r][c]                                                # càng gần đích (theo đường đi thật) càng tốt
        return value + random.random()

    move = max(observation['legalMoves'], key=score)
    return {'from': move['from'], 'to': move['to']}
