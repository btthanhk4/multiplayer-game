#!/usr/bin/env python3
"""Đấu trường tại máy: thử bot của bạn trước khi nộp. Chỉ dùng thư viện chuẩn Python 3.10+.

    python local_arena.py bot.py                    # 4 ván với bot Thợ săn
    python local_arena.py bot.py --vs random --games 10
    python local_arena.py bot.py --vs khac.py       # đấu với một file bot khác
    python local_arena.py bot.py --trace            # in từng nước đi và bàn cờ

Luật, giới hạn thời gian và dữ liệu `observation` giống hệt Arena. Mỗi ván đổi màu luân phiên.
Kết quả ở đây chỉ để tham khảo: máy chấm thật chạy trong sandbox và có thể chậm hơn máy bạn.
"""
import argparse
import random
import runpy
import sys
import time
from pathlib import Path

BOARD_SIZE = 9
BEATS = {'rock': 'scissors', 'paper': 'rock', 'scissors': 'paper'}
GOALS = {'p1': (0, 8), 'p2': (8, 0)}
FORMATION = [(7, 0, 'rock'), (6, 1, 'rock'), (8, 2, 'rock'),
             (8, 1, 'paper'), (7, 2, 'paper'), (7, 3, 'paper'),
             (6, 0, 'scissors'), (7, 1, 'scissors'), (6, 2, 'scissors')]
# Giới hạn của Arena (xem sổ tay)
TURN_SECONDS, GAME_SECONDS, MAX_PLIES, QUIET_LIMIT = 3.0, 60.0, 400, 80
LETTER = {'rock': 'Đ', 'paper': 'L', 'scissors': 'K'}


def square_name(row, col):
    return f"{'abcdefghi'[col]}{BOARD_SIZE - row}"


def new_game():
    board = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for row, col, kind in FORMATION:
        board[row][col] = {'owner': 'p1', 'type': kind}
        board[8 - row][8 - col] = {'owner': 'p2', 'type': kind}
    return {'board': board, 'turn': 'p1', 'winner': None, 'winReason': None, 'revision': 0, 'quietPly': 0,
            'repetitions': {position_key(board, 'p1'): 1}, 'lastMove': None}


def position_key(board, turn):
    return turn + ':' + '/'.join(''.join(f"{p['owner']}{p['type'][0]}" if p else '.' for p in line) for line in board)


def piece_counts(board):
    counts = {'p1': dict.fromkeys(BEATS, 0), 'p2': dict.fromkeys(BEATS, 0)}
    for line in board:
        for piece in line:
            if piece:
                counts[piece['owner']][piece['type']] += 1
    return counts


def step_moves(board, row, col):
    source = board[row][col]
    result = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            r, c = row + dr, col + dc
            if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
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
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
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
    if not (0 <= fr < 9 and 0 <= fc < 9):
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
    if (tr, tc) == GOALS[side]:
        winner, reason = side, 'goal'
    elif captured and piece_counts(board)[opponent][captured['type']] == 0:
        winner, reason = side, f"eliminate:{captured['type']}"
    next_turn = opponent
    quiet = 0 if captured else state['quietPly'] + 1
    repetitions = {} if captured else dict(state['repetitions'])
    key = position_key(board, next_turn)
    repetitions[key] = repetitions.get(key, 0) + 1
    if not winner and (repetitions[key] >= 3 or quiet >= QUIET_LIMIT):
        winner, reason = 'draw', 'repetition' if repetitions[key] >= 3 else 'quiet'
    return {'board': board, 'turn': side if winner else next_turn, 'winner': winner, 'winReason': reason,
            'revision': state['revision'] + 1, 'quietPly': quiet, 'repetitions': repetitions,
            'lastMove': {'side': side, 'from': list(source), 'to': list(target),
                         'notation': f"{square_name(fr, fc)}{'×' if captured else '–'}{square_name(tr, tc)}"}}, None


def observation(state, seed, time_left):
    return {'protocol_version': 2, 'board': state['board'], 'mySide': state['turn'], 'turn': state['turn'],
            'legalMoves': legal_moves(state), 'ply': state['revision'], 'quietPly': state['quietPly'],
            'repetitions': state['repetitions'], 'seed': seed, 'timeLeft': round(time_left, 3),
            'turnLimit': TURN_SECONDS, 'maxPly': MAX_PLIES}


def show_board(state):
    lines = ['    a b c d e f g h i']
    for r, line in enumerate(state['board']):
        cells = ' '.join((LETTER[p['type']].lower() if p['owner'] == 'p1' else LETTER[p['type']]) if p else '·' for p in line)
        lines.append(f'{BOARD_SIZE - r:>2}  {cells}  {BOARD_SIZE - r}')
    lines.append('    a b c d e f g h i      chữ thường = Xanh (p1), chữ hoa = Đỏ (p2)')
    return '\n'.join(lines)


# ---- bot có sẵn để làm đối thủ -------------------------------------------------

def bot_first(obs):
    move = obs['legalMoves'][0]
    return {'from': move['from'], 'to': move['to']}


def bot_random(obs):
    move = random.choice(obs['legalMoves'])
    return {'from': move['from'], 'to': move['to']}


def bot_hunter(obs):
    goal = GOALS[obs['mySide']]

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


def play(first, second, seed, trace=False):
    players = {'p1': first, 'p2': second}
    budget = {'p1': GAME_SECONDS, 'p2': GAME_SECONDS}
    slowest = {'p1': 0.0, 'p2': 0.0}
    spent = {'p1': 0.0, 'p2': 0.0}
    state = new_game()
    for side, player in players.items():
        try:
            player.start(seed + (side == 'p2'))
        except Fault as fault:
            return 'p2' if side == 'p1' else 'p1', f'technical: {fault}', 0, slowest, spent
    plies = 0
    while not state['winner']:
        moves = legal_moves(state)
        if not moves:
            return 'draw', 'no_legal_moves', plies, slowest, spent
        if plies >= MAX_PLIES:
            return 'draw', 'ply_limit', plies, slowest, spent
        side = state['turn']
        other = 'p2' if side == 'p1' else 'p1'
        started = time.perf_counter()
        try:
            answer = players[side].move(observation(state, seed + plies, budget[side]))
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
    parser.add_argument('--trace', action='store_true', help='in từng nước đi')
    args = parser.parse_args()
    mine, theirs = Player(*load_bot(args.bot)), Player(*load_bot(args.vs))
    score = {'win': 0, 'draw': 0, 'loss': 0}
    print(f'{mine.name} (bạn)  vs  {theirs.name}   ·   {args.games} ván, đổi màu luân phiên\n')
    for game in range(args.games):
        mine_first = game % 2 == 0
        first, second = (mine, theirs) if mine_first else (theirs, mine)
        winner, reason, plies, slowest, spent = play(first, second, args.seed + game * 101, args.trace)
        mine_side = 'p1' if mine_first else 'p2'
        result = 'draw' if winner == 'draw' else 'win' if winner == mine_side else 'loss'
        score[result] += 1
        label = {'win': 'THẮNG', 'draw': 'HÒA', 'loss': 'THUA'}[result]
        print(f"ván {game + 1} ({'Xanh' if mine_first else 'Đỏ '}): {label:<5} {reason}, {plies} nửa lượt, "
              f"nước chậm nhất của bạn {slowest[mine_side]*1000:.0f} ms, tổng {spent[mine_side]:.1f}s")
    print(f"\nKết quả: {score['win']} thắng · {score['draw']} hòa · {score['loss']} thua")


if __name__ == '__main__':
    main()
