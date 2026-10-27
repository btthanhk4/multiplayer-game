"""Bot tùy chỉnh: đổi "tính cách" chỉ bằng cách sửa 4 con số ngay bên dưới, không cần viết thêm code.

Mỗi lượt, bot chấm điểm mọi nước đi hợp lệ rồi chọn nước điểm cao nhất:
  chạm ô đích  = thắng ngay (điểm rất lớn, luôn được chọn)
  ăn quân      = + CAPTURE điểm
  bước vào chỗ bị khắc chế = - SAFETY điểm cho mỗi quân đối phương đứng cạnh có thể ăn ta
  còn xa đích  = - CHASE điểm cho mỗi bước đường còn phải đi (đi vòng vật cản, tính bằng đường thật)
Chạy đúng trên mọi bản đồ (bàn 8×8 hay 9×9, có hoặc không có vật cản).
"""
import random
from collections import deque

# ---- CHỈNH Ở ĐÂY: số lớn hơn nghĩa là bot coi việc đó quan trọng hơn ----
CHASE_GOAL = 1     # tiến về ô đích (0–10)
CAPTURE = 12       # ăn quân đối phương (0–30)
SAFETY = 10        # tránh bước vào ô bị quân đối phương khắc chế (0–30)
RANDOMNESS = 1     # độ khó đoán: 0 là luôn đi nước tốt nhất, số lớn thì hay đi nước khác (0–20)
# -------------------------------------------------------------------------

STEPS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
PREDATOR = {'scissors': 'rock', 'paper': 'scissors', 'rock': 'paper'}      # loại ăn được quân này


def distance_map(size, obstacles, goal):
    """dist[r][c] = số nước ít nhất từ ô (r, c) tới `goal`, đi 8 hướng, không đi qua vật cản."""
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
        """Số quân đối phương đứng cạnh ô (r, c) và khắc chế được quân `piece`."""
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
        last_of_kind = sum(1 for row in board for p in row if p and p['owner'] == side and p['type'] == piece['type']) == 1
        value = 1000000 if [r, c] == goal else 0
        value += CAPTURE * move['capture']
        value -= threats(r, c, piece) * SAFETY * (3 if last_of_kind else 1)     # quân cuối của một loại: thận trọng gấp ba
        value -= CHASE_GOAL * dist[r][c]
        return value + random.random() * RANDOMNESS

    move = max(observation['legalMoves'], key=score)
    return {'from': move['from'], 'to': move['to']}
