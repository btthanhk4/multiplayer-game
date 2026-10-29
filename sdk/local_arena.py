#!/usr/bin/env python3
"""Đấu trường tại máy: thử bot của bạn trước khi nộp. Chỉ dùng thư viện chuẩn Python 3.10+.

    python local_arena.py bot.py                    # 4 ván với bot Thợ săn
    python local_arena.py bot.py --vs random --games 10
    python local_arena.py bot.py --vs khac.py       # đấu với một file bot khác
    python local_arena.py bot.py --trace            # in từng nước đi và bàn cờ
    python local_arena.py bot.py --map river        # thử trên bản đồ khác (xem --help): bàn 8×8, vật cản...
    python local_arena.py bot.py --replay           # ghi replays/van-1.json...; xem lại ở Khán đài → Mở replay từ máy

Luật, giới hạn thời gian và dữ liệu `observation` giống hệt Arena. Mỗi ván đổi màu luân phiên.
Kết quả ở đây chỉ để tham khảo: máy chấm thật chạy trong sandbox và có thể chậm hơn máy bạn.
"""
import argparse
import json
import random
import runpy
import sys
import time
from pathlib import Path

BOARD_SIZE = 9
BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}
GOALS = {'p1': (0, 8), 'p2': (8, 0)}
# "Bản đồ": cách xếp quân ban đầu của Xanh (Đỏ là bản quay 180°) và, với vài bản đồ, kích thước bàn cờ và vật cản.
FORMATIONS = {
    'standard': [(7, 0, 'rock'), (6, 1, 'rock'), (8, 2, 'rock'),
                 (8, 1, 'paper'), (7, 2, 'paper'), (7, 3, 'paper'),
                 (6, 0, 'scissors'), (7, 1, 'scissors'), (6, 2, 'scissors')],
    'wall': [(8, 1, 'paper'), (8, 2, 'rock'), (8, 3, 'scissors'), (8, 4, 'paper'),
             (7, 0, 'scissors'), (7, 1, 'paper'), (7, 2, 'rock'), (7, 3, 'scissors'), (7, 4, 'rock')],
    'flank': [(6, 0, 'rock'), (6, 1, 'scissors'), (7, 0, 'paper'), (7, 1, 'rock'),
              (7, 3, 'scissors'), (7, 4, 'paper'), (8, 2, 'rock'), (8, 3, 'scissors'), (8, 4, 'paper')],
    'block': [(8, 1, 'rock'), (8, 2, 'paper'), (8, 3, 'scissors'),
              (7, 1, 'scissors'), (7, 2, 'rock'), (7, 3, 'paper'),
              (6, 2, 'paper'), (6, 3, 'scissors'), (6, 4, 'rock')],
}
ARENA8 = [(6, 0, 'rock'), (5, 1, 'rock'), (7, 2, 'rock'),
          (7, 1, 'paper'), (6, 2, 'paper'), (6, 3, 'paper'),
          (5, 0, 'scissors'), (6, 1, 'scissors'), (5, 2, 'scissors')]
# name -> (kích thước bàn, quân của Xanh, vật cản). Phải khớp từng ô với LAYOUTS trong rules.js.
LAYOUTS = {
    **{name: (9, pieces, []) for name, pieces in FORMATIONS.items()},
    'arena8': (8, ARENA8, []),
    'pillars': (9, FORMATIONS['standard'], [(3, 3), (3, 5), (5, 3), (5, 5)]),
    'river': (9, FORMATIONS['standard'], [(4, 0), (4, 1), (4, 2), (4, 4), (4, 6), (4, 7), (4, 8)]),
    'maze8': (8, ARENA8, [(3, 2), (3, 4), (4, 3), (4, 5)]),
}
TRIANGLE = [(4, 0, 'rock'), (7, 0, 'rock'), (6, 1, 'rock'),
            (5, 0, 'paper'), (7, 1, 'paper'), (5, 2, 'paper'),
            (6, 0, 'scissors'), (5, 1, 'scissors'), (6, 2, 'scissors')]


def cells_where(keep, size=9):
    return [(r, c) for r in range(size) for c in range(size) if keep(r, c)]


# Bàn có hình dạng: name -> (ô ngoài bàn, cách đối xứng). Ô ngoài bàn với luật và với bot y hệt vật cản.
# Tam giác lật qua đường chéo nối hai ô đích (đổi hàng với cột) thay vì quay 180°.
LAYOUTS.update({
    'hexagon': (9, FORMATIONS['standard'], []),
    'hourglass': (9, FORMATIONS['wall'], []),
    'lake': (9, FORMATIONS['standard'], []),
    'triangle': (9, TRIANGLE, []),
    'zigzag': (9, FORMATIONS['standard'], cells_where(lambda r, c: r == 3 and c <= 6) + cells_where(lambda r, c: r == 5 and c >= 2)),
})
SHAPES = {
    'hexagon': (cells_where(lambda r, c: r + c <= 3 or r + c >= 13), 'rotate'),
    'hourglass': (cells_where(lambda r, c: min(c, 8 - c) < 3 - abs(r - 4)), 'rotate'),
    'lake': (cells_where(lambda r, c: abs(r - 4) + abs(c - 4) <= 2), 'rotate'),
    'triangle': (cells_where(lambda r, c: r + c > 8), 'transpose'),
}
MAP_NAMES = list(LAYOUTS)            # bản đồ cố định; thêm `random` và `chaos` (mỗi ván một bàn khác theo seed)
WALL = {'owner': None, 'type': 'wall'}
VOID = {'owner': None, 'type': 'wall', 'void': True}


def lcg(seed_hex):
    """Bộ sinh giống rules.js: LCG 32 bit, lấy các bit cao."""
    state = int(seed_hex, 16) & 0xFFFFFFFF

    def step():
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state

    def shuffle(items):
        for i in range(len(items) - 1, 0, -1):
            j = (step() >> 8) % (i + 1)
            items[i], items[j] = items[j], items[i]
        return items
    return step, shuffle


def random_formation(seed_hex):
    """`random:<hex>` maps: pseudo-random but identical to rules.js for the same seed."""
    _, shuffle = lcg(seed_hex)
    cells = [(r, c) for r in range(6, 9) for c in range(0, 5) if not (r == 8 and c == 0)]
    types = shuffle(['rock'] * 3 + ['paper'] * 3 + ['scissors'] * 3)
    shuffle(cells)
    return [(cells[i][0], cells[i][1], types[i]) for i in range(9)]


def open_connected(size, walls):
    blocked = {r * size + c for r, c in walls}
    seen, queue = {0}, [0]
    while queue:
        cell = queue.pop()
        row, col = divmod(cell, size)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size and r * size + c not in blocked and r * size + c not in seen:
                    seen.add(r * size + c)
                    queue.append(r * size + c)
    return len(seen) == size * size - len(blocked)


def chaos_layout(seed_hex):
    """`chaos:<hex>`: bàn 8×8 hoặc 9×9, quân ngẫu nhiên và 2–4 cặp vật cản đối xứng; khớp rules.js."""
    step, shuffle = lcg(seed_hex)
    size = 9 if (step() >> 8) % 2 else 8
    cells = [(r, c) for r in range(size - 3, size) for c in range(0, 5) if not (r == size - 1 and c == 0)]
    types = shuffle(['rock'] * 3 + ['paper'] * 3 + ['scissors'] * 3)
    shuffle(cells)
    pieces = [(cells[i][0], cells[i][1], types[i]) for i in range(9)]
    candidates = [(r, c) for r in range(3, size - 3) for c in range(size)
                  if r * size + c < (size - 1 - r) * size + (size - 1 - c)]
    shuffle(candidates)
    pairs = 2 + (step() >> 8) % 3

    def walls_of(count):
        return [cell for r, c in candidates[:count] for cell in ((r, c), (size - 1 - r, size - 1 - c))]
    walls = walls_of(pairs)
    while walls and not open_connected(size, walls):
        pairs -= 1
        walls = walls_of(pairs)
    return size, pieces, walls


def layout_of(name='standard'):
    import re
    if isinstance(name, str):
        if re.fullmatch(r'random:[0-9a-f]{1,8}', name):
            return 9, random_formation(name[7:]), []
        if re.fullmatch(r'chaos:[0-9a-f]{1,8}', name):
            return chaos_layout(name[6:])
    if name not in LAYOUTS:
        raise ValueError(f'Bản đồ không tồn tại: {name}')
    return LAYOUTS[name]


def formation_of(name='standard'):
    return layout_of(name)[1]


def goal_of(side, size=BOARD_SIZE):
    return (0, size - 1) if side == 'p1' else (size - 1, 0)
# Giới hạn của Arena (xem sổ tay)
TURN_SECONDS, GAME_SECONDS, MAX_PLIES, QUIET_LIMIT = 3.0, 60.0, 400, 80
LETTER = {'rock': 'Đ', 'paper': 'L', 'scissors': 'K'}


def square_name(row, col, size=BOARD_SIZE):
    return f"{'abcdefghi'[col]}{size - row}"


def new_game(map_name='standard'):
    size, pieces, walls = layout_of(map_name)
    voids, mirror = SHAPES.get(map_name, ([], 'rotate'))
    board = [[None] * size for _ in range(size)]
    for row, col, kind in pieces:
        board[row][col] = {'owner': 'p1', 'type': kind}
        r, c = (col, row) if mirror == 'transpose' else (size - 1 - row, size - 1 - col)
        board[r][c] = {'owner': 'p2', 'type': kind}
    for row, col in walls:
        board[row][col] = WALL
    for row, col in voids:
        board[row][col] = VOID
    return {'board': board, 'turn': 'p1', 'winner': None, 'winReason': None, 'revision': 0, 'quietPly': 0,
            'repetitions': {position_key(board, 'p1'): 1}, 'lastMove': None}


def position_key(board, turn):
    return turn + ':' + '/'.join(''.join('#' if p and p['type'] == 'wall' else f"{p['owner']}{p['type'][0]}" if p else '.'
                                         for p in line) for line in board)


def piece_counts(board):
    counts = {'p1': dict.fromkeys(BEATS, 0), 'p2': dict.fromkeys(BEATS, 0)}
    for line in board:
        for piece in line:
            if piece and piece['owner']:
                counts[piece['owner']][piece['type']] += 1
    return counts


def nearest_to_goal(board, side):
    """Số nước (8 hướng, đi vòng vật cản, bỏ qua quân) từ quân gần ô đích nhất tới ô đích của chính nó."""
    size = len(board)
    goal_row, goal_col = goal_of(side, size)
    distance = {(goal_row, goal_col): 0}
    queue = [(goal_row, goal_col)]
    for row, col in queue:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size and (r, c) not in distance and not (board[r][c] and board[r][c]['type'] == 'wall'):
                    distance[(r, c)] = distance[(row, col)] + 1
                    queue.append((r, c))
    return min(distance.get((r, c), float('inf')) for r in range(size) for c in range(size)
               if board[r][c] and board[r][c]['owner'] == side)


def adjudicate(board):
    """Hết thế: nhiều quân hơn thắng; bằng quân thì quân gần ô đích hơn thắng; vẫn bằng thì hòa."""
    counts = piece_counts(board)
    total = {side: sum(counts[side].values()) for side in counts}
    if total['p1'] != total['p2']:
        return ('p1' if total['p1'] > total['p2'] else 'p2'), 'material'
    near = {side: nearest_to_goal(board, side) for side in ('p1', 'p2')}
    if near['p1'] != near['p2']:
        return ('p1' if near['p1'] < near['p2'] else 'p2'), 'advance'
    return 'draw', 'level'


def adjudicated(board, cause):
    winner, basis = adjudicate(board)
    return winner, cause if winner == 'draw' else f'{basis}:{cause}'


def step_moves(board, row, col):
    source = board[row][col]
    result = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            r, c = row + dr, col + dc
            if not (0 <= r < len(board) and 0 <= c < len(board[r])):
                continue
            target = board[r][c]
            if target is None:
                result.append((r, c, False))
            elif target['owner'] != source['owner'] and BEATS[source['type']] == target['type']:
                result.append((r, c, True))
    return result


def legal_moves(state):
    if state['winner']:
        return []
    moves = []
    for r in range(len(state['board'])):
        for c in range(len(state['board'][r])):
            piece = state['board'][r][c]
            if piece and piece['owner'] == state['turn']:
                moves += [{'from': [r, c], 'to': [nr, nc], 'capture': cap} for nr, nc, cap in step_moves(state['board'], r, c)]
    return moves


def apply_move(state, side, source, target):
    """Trả về (trạng_thái_mới, None) hoặc (None, lỗi). Không sửa trạng thái cũ."""
    if state['winner']:
        return None, 'Ván đấu đã kết thúc.'
    if state['turn'] != side:
        return None, 'Chưa tới lượt của bạn.'
    if not (isinstance(source, (list, tuple)) and isinstance(target, (list, tuple)) and len(source) == 2 == len(target)
            and all(type(x) is int for x in (*source, *target))):
        return None, 'Tọa độ không hợp lệ.'
    (fr, fc), (tr, tc) = source, target
    if not (0 <= fr < len(state['board']) and 0 <= fc < len(state['board'])):
        return None, 'Đó không phải quân của bạn.'
    piece = state['board'][fr][fc]
    if not piece or piece['owner'] != side:
        return None, 'Đó không phải quân của bạn.'
    if (tr, tc, False) not in step_moves(state['board'], fr, fc) and (tr, tc, True) not in step_moves(state['board'], fr, fc):
        return None, 'Nước đi không hợp lệ.'
    captured = state['board'][tr][tc]
    board = [line[:] for line in state['board']]
    board[fr][fc], board[tr][tc] = None, piece
    opponent = 'p2' if side == 'p1' else 'p1'
    winner = reason = None
    if (tr, tc) == goal_of(side, len(board)):
        winner, reason = side, 'goal'
    elif captured and piece_counts(board)[opponent][captured['type']] == 0:
        winner, reason = side, f"eliminate:{captured['type']}"
    next_turn = opponent
    quiet = 0 if captured else state['quietPly'] + 1
    repetitions = {} if captured else dict(state['repetitions'])
    key = position_key(board, next_turn)
    repetitions[key] = repetitions.get(key, 0) + 1
    if not winner and (repetitions[key] >= 3 or quiet >= QUIET_LIMIT):
        winner, reason = adjudicated(board, 'repetition' if repetitions[key] >= 3 else 'quiet')
    return {'board': board, 'turn': side if winner else next_turn, 'winner': winner, 'winReason': reason,
            'revision': state['revision'] + 1, 'quietPly': quiet, 'repetitions': repetitions,
            'lastMove': {'side': side, 'from': list(source), 'to': list(target),
                         'notation': f"{square_name(fr, fc, len(board))}{'×' if captured else '–'}{square_name(tr, tc, len(board))}"}}, None


def observation(state, seed, time_left, map_name='standard'):
    size = len(state['board'])
    obstacles = [[r, c] for r, line in enumerate(state['board']) for c, cell in enumerate(line) if cell and cell['type'] == 'wall']
    board = [[None if cell and cell['type'] == 'wall' else cell for cell in line] for line in state['board']]
    other = 'p2' if state['turn'] == 'p1' else 'p1'
    return {'protocol_version': 2, 'map': map_name, 'size': size, 'obstacles': obstacles, 'board': board,
            'goal': list(goal_of(state['turn'], size)), 'opponentGoal': list(goal_of(other, size)),
            'mySide': state['turn'], 'turn': state['turn'],
            'legalMoves': legal_moves(state), 'ply': state['revision'], 'quietPly': state['quietPly'],
            'repetitions': state['repetitions'], 'seed': seed, 'timeLeft': round(time_left, 3),
            'turnLimit': TURN_SECONDS, 'maxPly': MAX_PLIES}


def show_board(state):
    size = len(state['board'])
    files = ' '.join('abcdefghi'[:size])
    lines = [f'    {files}']
    for r, line in enumerate(state['board']):
        cells = ' '.join(' ' if p and p.get('void') else '#' if p and p['type'] == 'wall' else (LETTER[p['type']].lower() if p['owner'] == 'p1' else LETTER[p['type']])
                         if p else '·' for p in line)
        lines.append(f'{size - r:>2}  {cells}  {size - r}')
    shaped = any(p and p.get('void') for line in state['board'] for p in line)
    lines.append(f'    {files}      chữ thường = Xanh (p1), chữ hoa = Đỏ (p2), # = vật cản' + (', ô trống = ngoài bàn' if shaped else ''))
    return '\n'.join(lines)


# ---- bot có sẵn để làm đối thủ -------------------------------------------------

def bot_first(obs):
    move = obs['legalMoves'][0]
    return {'from': move['from'], 'to': move['to']}


def bot_random(obs):
    move = random.choice(obs['legalMoves'])
    return {'from': move['from'], 'to': move['to']}


def bot_hunter(obs):
    goal = obs['goal']

    def score(move):
        distance = max(abs(move['to'][0] - goal[0]), abs(move['to'][1] - goal[1]))
        return (1000 if tuple(move['to']) == goal else 0) + 20 * move['capture'] - distance + random.random()
    move = max(obs['legalMoves'], key=score)
    return {'from': move['from'], 'to': move['to']}


BUILTIN = {'first': bot_first, 'random': bot_random, 'hunter': bot_hunter}


def load_bot(spec):
    if spec in BUILTIN:
        return spec, BUILTIN[spec], None
    path = Path(spec)
    if not path.is_file():
        sys.exit(f'Không thấy file bot: {spec}')
    return path.stem, None, path


class Player:
    """Nạp bot như Arena: một lần mỗi ván, biến toàn cục được giữ giữa các nước."""

    def __init__(self, name, function, path):
        self.name, self.function, self.path = name, function, path

    def start(self, seed):
        random.seed(seed)
        if self.path:
            started = time.perf_counter()
            try:
                self.function = runpy.run_path(str(self.path), run_name='ott_bot')['choose_move']
            except BaseException as error:  # noqa: BLE001
                raise Fault(f'Không nạp được bot: {type(error).__name__}: {error}')
            if time.perf_counter() - started > 5:
                raise Fault('Nạp mã quá 5 giây')

    def move(self, obs):
        try:
            return self.function(obs)
        except BaseException as error:  # noqa: BLE001
            raise Fault(f'Bot lỗi thực thi: {type(error).__name__}: {error}')


class Fault(Exception):
    pass


def play(first, second, seed, trace=False, map_name='standard', record=None):
    players = {'p1': first, 'p2': second}
    budget = {'p1': GAME_SECONDS, 'p2': GAME_SECONDS}
    slowest = {'p1': 0.0, 'p2': 0.0}
    spent = {'p1': 0.0, 'p2': 0.0}
    state = new_game(map_name)
    for side, player in players.items():
        try:
            player.start(seed + (side == 'p2'))
        except Fault as fault:
            return 'p2' if side == 'p1' else 'p1', f'technical: {fault}', 0, slowest, spent
    plies = 0
    while not state['winner']:
        moves = legal_moves(state)
        if not moves:
            return (*adjudicated(state['board'], 'no_legal_moves'), plies, slowest, spent)
        if plies >= MAX_PLIES:
            return (*adjudicated(state['board'], 'ply_limit'), plies, slowest, spent)
        side = state['turn']
        other = 'p2' if side == 'p1' else 'p1'
        started = time.perf_counter()
        try:
            answer = players[side].move(observation(state, seed + plies, budget[side], map_name))
        except Fault as fault:
            return other, f'technical: {fault}', plies, slowest, spent
        used = time.perf_counter() - started
        budget[side] -= used
        spent[side] += used
        slowest[side] = max(slowest[side], used)
        if used > TURN_SECONDS:
            return other, f'technical: quá {TURN_SECONDS:g} giây ở một nước ({used:.1f}s)', plies, slowest, spent
        if budget[side] < 0:
            return other, 'technical: hết quỹ 60 giây suy nghĩ của ván', plies, slowest, spent
        if not isinstance(answer, dict) or 'from' not in answer or 'to' not in answer:
            return other, "technical: kết quả phải là {'from': [r, c], 'to': [r, c]}", plies, slowest, spent
        new_state, error = apply_move(state, side, answer['from'], answer['to'])
        if error:
            return other, f'technical: {error} {answer}', plies, slowest, spent
        state, plies = new_state, plies + 1
        if record is not None:
            record.append({key: state['lastMove'][key] for key in ('side', 'from', 'to', 'notation')})
        if trace:
            print(f"{plies:>3}. {side} {state['lastMove']['notation']}  ({used*1000:.0f} ms)")
            print(show_board(state), end='\n\n')
    return state['winner'], state['winReason'], plies, slowest, spent


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):   # console Windows mặc định không in được tiếng Việt
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Thử bot Arena tại máy.')
    parser.add_argument('bot', help='file .py của bạn (có hàm choose_move)')
    parser.add_argument('--vs', default='hunter', help='first | random | hunter | đường dẫn file bot khác')
    parser.add_argument('--games', type=int, default=4, help='số ván (đổi màu luân phiên)')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--map', default='standard',
                        help=' | '.join(MAP_NAMES) + ' | random | chaos (hai loại cuối khác nhau mỗi cặp ván)')
    parser.add_argument('--trace', action='store_true', help='in từng nước đi')
    parser.add_argument('--replay', nargs='?', const='replays', metavar='THƯ_MỤC',
                        help='ghi mỗi ván thành một file JSON (mặc định replays/) để xem lại ở Khán đài của Arena')
    args = parser.parse_args()
    mine, theirs = Player(*load_bot(args.bot)), Player(*load_bot(args.vs))
    score = {'win': 0, 'draw': 0, 'loss': 0}
    print(f'{mine.name} (bạn)  vs  {theirs.name}   ·   {args.games} ván, đổi màu luân phiên\n')
    for game in range(args.games):
        mine_first = game % 2 == 0
        first, second = (mine, theirs) if mine_first else (theirs, mine)
        seeded = args.map in ('random', 'chaos')
        map_name = f'{args.map}:{(args.seed + game // 2 * 7919) & 0xFFFFFFF:x}' if seeded else args.map
        record = [] if args.replay else None
        winner, reason, plies, slowest, spent = play(first, second, args.seed + game * 101, args.trace, map_name, record)
        mine_side = 'p1' if mine_first else 'p2'
        result = 'draw' if winner == 'draw' else 'win' if winner == mine_side else 'loss'
        score[result] += 1
        label = {'win': 'THẮNG', 'draw': 'HÒA', 'loss': 'THUA'}[result]
        print(f"ván {game + 1} ({'Xanh' if mine_first else 'Đỏ '}, {map_name}): {label:<5} {reason}, {plies} nửa lượt, "
              f"nước chậm nhất của bạn {slowest[mine_side]*1000:.0f} ms, tổng {spent[mine_side]:.1f}s")
        if record is not None:
            print(f'    replay: {save_replay(args.replay, game + 1, map_name, first, second, mine, winner, reason, record, spent)}')
    print(f"\nKết quả: {score['win']} thắng · {score['draw']} hòa · {score['loss']} thua")
    if args.replay:
        print('Xem lại: mở Arena → Khán đài → "Mở replay từ máy" rồi chọn file .json. '
              'File chỉ được đọc trên trình duyệt, không gửi lên máy chủ.')


def save_replay(folder, number, map_name, first, second, mine, winner, reason, moves, spent):
    """Một ván thành JSON cùng dạng replay của Arena (map, winner, reason, moves) để Khán đài phát lại."""
    names = {'p1': first.name, 'p2': second.name}
    names['p1' if first is mine else 'p2'] += ' (bạn)'
    technical = reason.startswith('technical')
    replay = {'format': 'ottv2-replay', 'version': 1, 'map': map_name, 'p1': names['p1'], 'p2': names['p2'],
              'winner': winner, 'reason': 'technical:local' if technical else reason,
              'detail': reason.split(':', 1)[1].strip() if technical else None,
              'moves': moves, 'time_used': {side: round(spent[side], 3) for side in ('p1', 'p2')}}
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    path /= f'van-{number}.json'
    path.write_text(json.dumps(replay, ensure_ascii=False), encoding='utf-8')
    return path


if __name__ == '__main__':
    main()
