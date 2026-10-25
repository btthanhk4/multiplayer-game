import test from 'node:test';
import assert from 'node:assert/strict';
import { applyMove, legalMoves, makeBoard, newGame, pieceCounts } from '../rules.js';

test('hòa ở lần lặp thế thứ ba, gồm bên đến lượt', () => {
  let state = newGame();
  const cycle = [
    ['p1', [6,0], [5,0]], ['p2', [2,8], [3,8]],
    ['p1', [5,0], [6,0]], ['p2', [3,8], [2,8]],
  ];
  for (let iteration=0;iteration<2;iteration++) for (const [side,from,to] of cycle) {
    const result = applyMove(state,side,from,to);
    assert.equal(result.ok,true);
    state=result.state;
  }
  assert.equal(state.winner,'draw');
  assert.equal(state.winReason,'repetition');
  assert.equal(state.revision,8);
});

test('hòa khi nước thứ 80 liên tiếp không ăn quân', () => {
  const state = newGame();
  state.quietPly=79;
  const result=applyMove(state,'p1',[6,0],[5,0]);
  assert.equal(result.state.winner,'draw');
  assert.equal(result.state.winReason,'quiet');
});

test('đội hình 9×9 đối xứng và có ba quân mỗi loại', () => {
  const board = makeBoard();
  assert.equal(board.length, 9);
  for (let row = 0; row < 9; row += 1) {
    assert.equal(board[row].length, 9);
    for (let col = 0; col < 9; col += 1) {
      const here = board[row][col];
      const mirrored = board[8 - row][8 - col];
      if (here) {
        assert.equal(mirrored?.type, here.type);
        assert.notEqual(mirrored.owner, here.owner);
      }
    }
  }
  const counts = pieceCounts(board);
  assert.deepEqual(counts.p1, { rock: 3, paper: 3, scissors: 3 });
  assert.deepEqual(counts.p2, { rock: 3, paper: 3, scissors: 3 });
  assert.equal(board[0][8], null);
  assert.equal(board[8][0], null);
});

test('di chuyển tám hướng, quân cùng loại chặn nhau', () => {
  const board = Array.from({ length: 9 }, () => Array(9).fill(null));
  board[4][4] = { owner: 'p1', type: 'rock' };
  board[3][4] = { owner: 'p2', type: 'rock' };
  board[3][5] = { owner: 'p2', type: 'scissors' };
  board[4][5] = { owner: 'p2', type: 'paper' };
  board[5][4] = { owner: 'p1', type: 'paper' };
  const moves = legalMoves(board, 4, 4);
  assert.equal(moves.length, 5);
  assert.deepEqual(moves.find((move) => move.row === 3 && move.col === 5)?.kind, 'capture');
  assert.equal(moves.some((move) => move.row === 3 && move.col === 4), false);
  assert.equal(moves.some((move) => move.row === 4 && move.col === 5), false);
  assert.equal(moves.some((move) => move.row === 5 && move.col === 4), false);
});

test('chỉ đúng phe ở đúng lượt được đi', () => {
  const game = newGame();
  assert.equal(applyMove(game, 'p2', [0, 6], [1, 5]).ok, false);
  assert.equal(applyMove(game, 'p1', [7, 0], [6, 1]).ok, false);
  const result = applyMove(game, 'p1', [6, 0], [5, 0]);
  assert.equal(result.ok, true);
  assert.equal(result.state.turn, 'p2');
  assert.equal(result.state.revision, 1);
  assert.equal(game.revision, 0);
});

test('ăn quân cuối cùng của một loại sẽ thắng', () => {
  const game = newGame();
  game.board = Array.from({ length: 9 }, () => Array(9).fill(null));
  game.board[4][4] = { owner: 'p1', type: 'rock' };
  game.board[3][5] = { owner: 'p2', type: 'scissors' };
  game.board[0][0] = { owner: 'p2', type: 'paper' };
  const result = applyMove(game, 'p1', [4, 4], [3, 5]);
  assert.equal(result.ok, true);
  assert.equal(result.state.winner, 'p1');
  assert.equal(result.state.winReason, 'eliminate:scissors');
});

test('đi tới ô đích i9 hoặc a1 sẽ thắng', () => {
  for (const [side, from, to] of [
    ['p1', [1, 7], [0, 8]],
    ['p2', [7, 1], [8, 0]],
  ]) {
    const game = newGame();
    game.board = Array.from({ length: 9 }, () => Array(9).fill(null));
    game.board[from[0]][from[1]] = { owner: side, type: 'paper' };
    game.turn = side;
    const result = applyMove(game, side, from, to);
    assert.equal(result.ok, true);
    assert.equal(result.state.winner, side);
    assert.equal(result.state.winReason, 'goal');
  }
});

// ---- bàn 8×8 và vật cản --------------------------------------------------------------------------------------------

import { MAP_NAMES, WALL, adjudicate, goalOf, layoutOf, squareName } from '../rules.js';

const SEEDS = Array.from({ length: 300 }, (_, i) => ((i + 1) * 2654435761 >>> 0).toString(16));
const ALL_MAPS = [...MAP_NAMES, ...SEEDS.map((s) => `random:${s}`), ...SEEDS.map((s) => `chaos:${s}`)];
const isWallCell = (cell) => cell?.type === 'wall';

function connected(board) {
  const size = board.length;
  const open = [];
  board.forEach((line, r) => line.forEach((cell, c) => { if (!isWallCell(cell)) open.push(r * size + c); }));
  const seen = new Set([open[0]]);
  const queue = [open[0]];
  while (queue.length) {
    const cell = queue.pop();
    for (let dr = -1; dr <= 1; dr += 1) for (let dc = -1; dc <= 1; dc += 1) {
      const r = Math.floor(cell / size) + dr, c = cell % size + dc;
      if (r < 0 || c < 0 || r >= size || c >= size || isWallCell(board[r][c]) || seen.has(r * size + c)) continue;
      seen.add(r * size + c); queue.push(r * size + c);
    }
  }
  return seen.size === open.length;
}

test('mọi bản đồ: đối xứng quay 180°, 3 quân mỗi loại, đích trống, hai bên cách nhau, bàn liền một khối', () => {
  for (const map of ALL_MAPS) {
    const board = makeBoard(map), n = board.length;
    assert.ok(n === 8 || n === 9, map);
    assert.ok(board.every((line) => line.length === n), map);
    for (let r = 0; r < n; r += 1) for (let c = 0; c < n; c += 1) {
      const a = board[r][c], b = board[n - 1 - r][n - 1 - c];
      assert.equal(isWallCell(a), isWallCell(b), `${map} vật cản đối xứng ${r},${c}`);
      if (a && !isWallCell(a)) assert.deepEqual([b.owner, b.type], [a.owner === 'p1' ? 'p2' : 'p1', a.type], map);
    }
    const counts = pieceCounts(board);
    for (const side of ['p1', 'p2']) assert.deepEqual(counts[side], { rock: 3, paper: 3, scissors: 3 }, map);
    for (const side of ['p1', 'p2']) assert.equal(board[goalOf(side, n)[0]][goalOf(side, n)[1]], null, `${map}: đích trống`);
    assert.ok(connected(board), `${map}: bàn phải liền một khối`);
    // Không có ăn quân ngay từ nước đầu: không quân nào của Xanh đứng sát quân Đỏ.
    for (let r = 0; r < n; r += 1) for (let c = 0; c < n; c += 1) {
      if (board[r][c]?.owner !== 'p1') continue;
      for (const step of legalMoves(board, r, c)) assert.equal(step.kind, 'move', `${map}: ăn quân ngay ở ${r},${c}`);
    }
  }
});

test('bản đồ cố định: kích thước và số vật cản đúng như mô tả', () => {
  const shape = Object.fromEntries(MAP_NAMES.map((m) => [m, [layoutOf(m).size, layoutOf(m).walls.length]]));
  assert.deepEqual(shape, {
    standard: [9, 0], wall: [9, 0], flank: [9, 0], block: [9, 0],
    arena8: [8, 0], pillars: [9, 4], river: [9, 7], maze8: [8, 4],
  });
});

test('bản đồ hỗn loạn: cùng seed cùng bàn, có cả 8×8 và 9×9, 2–4 cặp vật cản, seed khác thì khác', () => {
  assert.deepEqual(makeBoard('chaos:abc'), makeBoard('chaos:abc'));
  const sizes = new Set(), walls = new Set();
  for (const seed of SEEDS) { const l = layoutOf(`chaos:${seed}`); sizes.add(l.size); walls.add(l.walls.length); }
  assert.deepEqual([...sizes].sort(), [8, 9]);
  assert.ok([...walls].every((n) => [0, 2, 4, 6, 8].includes(n)) && walls.has(4) && walls.has(8));
  assert.notDeepEqual(makeBoard('chaos:1'), makeBoard('chaos:2'));
  assert.throws(() => makeBoard('chaos:xyz'));
  assert.throws(() => makeBoard('nope'));
});

test('vật cản không đi vào, không ăn được, không tính là quân và không đổi khi chơi', () => {
  const game = newGame('river');
  assert.equal(game.board[4][0], WALL);
  const lone = newGame();
  lone.board = Array.from({ length: 9 }, () => Array(9).fill(null));
  lone.board[5][3] = { owner: 'p1', type: 'rock' };
  lone.board[4][3] = WALL;
  lone.board[4][4] = { owner: 'p2', type: 'scissors' };
  lone.board[0][0] = { owner: 'p2', type: 'paper' };
  const targets = legalMoves(lone.board, 5, 3).map((m) => `${m.row},${m.col}`);
  assert.ok(!targets.includes('4,3'), 'không đi vào vật cản');
  assert.ok(targets.includes('4,4'), 'vẫn ăn được quân bên cạnh vật cản');
  assert.deepEqual(legalMoves(lone.board, 4, 3), [], 'vật cản không có nước đi');
  assert.equal(applyMove(lone, 'p1', [5, 3], [4, 3]).ok, false);
  assert.equal(applyMove(lone, 'p1', [4, 3], [3, 3]).ok, false);
  assert.deepEqual(pieceCounts(lone.board).p1, { rock: 1, paper: 0, scissors: 0 });
  const moved = applyMove(lone, 'p1', [5, 3], [4, 4]).state;
  assert.equal(moved.board[4][3], WALL);
});

test('bàn 8×8: ô đích, tên ô và thắng khi chạm đích', () => {
  assert.deepEqual([goalOf('p1', 8), goalOf('p2', 8), goalOf('p1'), goalOf('p2')], [[0, 7], [7, 0], [0, 8], [8, 0]]);
  assert.equal(squareName(0, 7, 8), 'h8');
  assert.equal(squareName(7, 0, 8), 'a1');
  for (const [side, from, to] of [['p1', [1, 6], [0, 7]], ['p2', [6, 1], [7, 0]]]) {
    const game = newGame('arena8');
    game.board = Array.from({ length: 8 }, () => Array(8).fill(null));
    game.board[from[0]][from[1]] = { owner: side, type: 'paper' };
    game.turn = side;
    const result = applyMove(game, side, from, to);
    assert.equal(result.ok, true);
    assert.deepEqual([result.state.winner, result.state.winReason], [side, 'goal']);
    assert.equal(result.state.lastMove.notation.endsWith(side === 'p1' ? 'h8' : 'a1'), true);
  }
  assert.equal(applyMove(newGame('arena8'), 'p1', [7, 1], [8, 1]).ok, false, 'ngoài bàn 8×8');
});

test('xử hết thế tính đường đi vòng vật cản, không phải khoảng cách thẳng', () => {
  const place = (walls) => {
    const game = newGame();
    game.board = Array.from({ length: 9 }, () => Array(9).fill(null));
    if (walls) for (const c of [0, 1, 2, 4, 6, 7, 8]) game.board[4][c] = WALL;
    game.board[5][8] = { owner: 'p1', type: 'rock' };   // thẳng tới (0,8) là 5, nhưng phải đi vòng qua cầu cột f: 7
    game.board[3][3] = { owner: 'p2', type: 'rock' };   // tới (8,0) là 5 và đường qua cầu cột d không bị cản
    return game;
  };
  assert.deepEqual(adjudicate(place(false)), { winner: 'draw', basis: 'level' }, 'bàn trống: 5 – 5');
  assert.deepEqual(adjudicate(place(true)), { winner: 'p2', basis: 'advance' }, 'có tường: Xanh 7, Đỏ 5');
});
