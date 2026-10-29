export const BOARD_SIZE = 9;
export const TYPES = ['rock', 'paper', 'scissors'];
export const TYPE_LABEL = { rock: 'Đấm', paper: 'Lá', scissors: 'Kéo' };
export const SIDE_LABEL = { p1: 'Xanh', p2: 'Đỏ' };
export const BEATS = { rock: 'scissors', paper: 'rock', scissors: 'paper' };
export const GOALS = { p1: [0, 8], p2: [8, 0] };
// Vật cản: ô đặc, không quân nào đi vào hay ăn được. Trong `board` nó là hằng số WALL (owner null).
export const WALL = Object.freeze({ owner: null, type: 'wall' });
export const isWall = (piece) => piece?.type === 'wall';
// Ô ngoài bàn của các bàn có hình dạng (lục giác, tam giác...): với luật và với bot nó y hệt vật cản, chỉ khác cách vẽ.
export const VOID = Object.freeze({ owner: null, type: 'wall', void: true });

// Ô đích của một bên trên bàn cờ `size` × `size`: Xanh ở góc trên phải, Đỏ ở góc dưới trái.
export function goalOf(side, size = BOARD_SIZE) {
  return side === 'p1' ? [0, size - 1] : [size - 1, 0];
}

// Quân Xanh xuất phát quanh a1; quân Đỏ quay 180° quanh tâm bàn cờ.
// Mỗi bên có đúng ba quân của từng loại. Hai ô đích lúc đầu để trống.
// "Bản đồ" là cách xếp quân ban đầu, và với vài bản đồ còn là kích thước bàn cờ (8×8), các ô vật cản và hình dạng bàn
// (ô ngoài bàn). Luật đi quân không đổi. Mọi bản đồ đối xứng quay 180° (riêng tam giác: lật qua đường chéo nối hai ô
// đích), nên hai bên luôn công bằng; ô đích lúc đầu để trống,
// hai bên cách nhau ít nhất 2 hàng trống và không có ăn quân ngay. Bản đồ 9×9 cũ giữ nguyên từng ô.
const FORMATIONS = {
  standard: [
    [7, 0, 'rock'], [6, 1, 'rock'], [8, 2, 'rock'],
    [8, 1, 'paper'], [7, 2, 'paper'], [7, 3, 'paper'],
    [6, 0, 'scissors'], [7, 1, 'scissors'], [6, 2, 'scissors'],
  ],
  wall: [
    [8, 1, 'paper'], [8, 2, 'rock'], [8, 3, 'scissors'], [8, 4, 'paper'],
    [7, 0, 'scissors'], [7, 1, 'paper'], [7, 2, 'rock'], [7, 3, 'scissors'], [7, 4, 'rock'],
  ],
  flank: [
    [6, 0, 'rock'], [6, 1, 'scissors'], [7, 0, 'paper'], [7, 1, 'rock'],
    [7, 3, 'scissors'], [7, 4, 'paper'], [8, 2, 'rock'], [8, 3, 'scissors'], [8, 4, 'paper'],
  ],
  block: [
    [8, 1, 'rock'], [8, 2, 'paper'], [8, 3, 'scissors'],
    [7, 1, 'scissors'], [7, 2, 'rock'], [7, 3, 'paper'],
    [6, 2, 'paper'], [6, 3, 'scissors'], [6, 4, 'rock'],
  ],
};
// Bàn 8×8: Xanh xếp ở ba hàng dưới cùng, cách Đỏ hai hàng trống ở giữa.
const ARENA8 = [
  [6, 0, 'rock'], [5, 1, 'rock'], [7, 2, 'rock'],
  [7, 1, 'paper'], [6, 2, 'paper'], [6, 3, 'paper'],
  [5, 0, 'scissors'], [6, 1, 'scissors'], [5, 2, 'scissors'],
];
const TRIANGLE = [
  [4, 0, 'rock'], [7, 0, 'rock'], [6, 1, 'rock'],
  [5, 0, 'paper'], [7, 1, 'paper'], [5, 2, 'paper'],
  [6, 0, 'scissors'], [5, 1, 'scissors'], [6, 2, 'scissors'],
];
function cellsWhere(size, keep) {
  const cells = [];
  for (let r = 0; r < size; r += 1) for (let c = 0; c < size; c += 1) if (keep(r, c)) cells.push([r, c]);
  return cells;
}
const LAYOUTS = {
  standard: { size: 9, pieces: FORMATIONS.standard, walls: [] },
  wall: { size: 9, pieces: FORMATIONS.wall, walls: [] },
  flank: { size: 9, pieces: FORMATIONS.flank, walls: [] },
  block: { size: 9, pieces: FORMATIONS.block, walls: [] },
  // 8×8, không vật cản: bàn nhỏ, hai bên chạm trán sớm.
  arena8: { size: 8, pieces: ARENA8, walls: [] },
  // Bốn trụ đá quanh tâm bàn cờ.
  pillars: { size: 9, pieces: FORMATIONS.standard, walls: [[3, 3], [3, 5], [5, 3], [5, 5]] },
  // Một bức tường ngang giữa bàn cờ, chỉ chừa hai cây cầu ở cột d và f.
  river: { size: 9, pieces: FORMATIONS.standard,
    walls: [[4, 0], [4, 1], [4, 2], [4, 4], [4, 6], [4, 7], [4, 8]] },
  // 8×8 với bốn vật cản ở giữa xếp chéo.
  maze8: { size: 8, pieces: ARENA8, walls: [[3, 2], [3, 4], [4, 3], [4, 5]] },
  // Bàn có hình dạng: `voids` là các ô ngoài bàn. Lục giác: khoét hai góc không có ô đích.
  hexagon: { size: 9, pieces: FORMATIONS.standard, walls: [], voids: cellsWhere(9, (r, c) => r + c <= 3 || r + c >= 13) },
  // Đồng hồ cát: hai tam giác chụm đỉnh, eo giữa chỉ rộng ba ô.
  hourglass: { size: 9, pieces: FORMATIONS.wall, walls: [],
    voids: cellsWhere(9, (r, c) => Math.min(c, 8 - c) < 3 - Math.abs(r - 4)) },
  // Hồ giữa: một hồ hình thoi 13 ô chiếm tâm bàn, phải đi vòng hai bên.
  lake: { size: 9, pieces: FORMATIONS.standard, walls: [], voids: cellsWhere(9, (r, c) => Math.abs(r - 4) + Math.abs(c - 4) <= 2) },
  // Tam giác: nửa bàn trên đường chéo, hai ô đích là hai đầu cạnh dài. Đỏ là ảnh của Xanh qua đường chéo đó
  // (đổi hàng với cột) thay vì quay 180°, vì tam giác không đối xứng khi quay.
  triangle: { size: 9, pieces: TRIANGLE, walls: [], voids: cellsWhere(9, (r, c) => r + c > 8), mirror: 'transpose' },
  // Ziczac: hai bức tường so le, đường tới đích uốn hình chữ S qua hàng giữa.
  zigzag: { size: 9, pieces: FORMATIONS.standard, walls: [...cellsWhere(9, (r, c) => r === 3 && c <= 6), ...cellsWhere(9, (r, c) => r === 5 && c >= 2)] },
};
export const MAP_NAMES = Object.keys(LAYOUTS);

// `random:<1-8 chữ số hex>`: cách xếp ngẫu nhiên nhưng xác định theo seed (cùng seed → cùng bàn cờ ở mọi nơi).
// Dùng bộ sinh LCG 32 bit và lấy các bit cao để cùng kết quả với bản Python của sdk/local_arena.py.
function lcg(seedHex) {
  let state = parseInt(seedHex, 16) >>> 0;
  const next = () => (state = (Math.imul(state, 1664525) + 1013904223) >>> 0);
  const shuffle = (list) => {
    for (let i = list.length - 1; i > 0; i -= 1) {
      const j = (next() >>> 8) % (i + 1);
      [list[i], list[j]] = [list[j], list[i]];
    }
    return list;
  };
  return { next, shuffle };
}

export function randomFormation(seedHex) {
  const { shuffle } = lcg(seedHex);
  const cells = [];
  for (let row = 6; row <= 8; row += 1) {
    for (let col = 0; col <= 4; col += 1) {
      if (!(row === 8 && col === 0)) cells.push([row, col]);
    }
  }
  const types = shuffle(['rock', 'rock', 'rock', 'paper', 'paper', 'paper', 'scissors', 'scissors', 'scissors']);
  shuffle(cells);
  return types.map((type, index) => [cells[index][0], cells[index][1], type]);
}

// Mọi ô còn lại của bàn cờ (trừ vật cản) phải nối liền nhau theo 8 hướng, nếu không bản đồ ngẫu nhiên bị loại bớt vật cản.
function openConnected(size, walls) {
  const blocked = new Set(walls.map(([r, c]) => r * size + c));
  const seen = new Set([0]);
  const queue = [0];
  while (queue.length) {
    const cell = queue.pop();
    const row = Math.floor(cell / size);
    const col = cell % size;
    for (let dr = -1; dr <= 1; dr += 1) {
      for (let dc = -1; dc <= 1; dc += 1) {
        const r = row + dr;
        const c = col + dc;
        if (r < 0 || r >= size || c < 0 || c >= size || blocked.has(r * size + c) || seen.has(r * size + c)) continue;
        seen.add(r * size + c);
        queue.push(r * size + c);
      }
    }
  }
  return seen.size === size * size - blocked.size;
}

// `chaos:<hex>`: bàn 8×8 hoặc 9×9, quân xếp ngẫu nhiên và 2–4 cặp vật cản đối xứng ở dải giữa bàn cờ.
export function chaosLayout(seedHex) {
  const { next, shuffle } = lcg(seedHex);
  const size = (next() >>> 8) % 2 ? 9 : 8;
  const cells = [];
  for (let row = size - 3; row < size; row += 1) {
    for (let col = 0; col <= 4; col += 1) {
      if (!(row === size - 1 && col === 0)) cells.push([row, col]);
    }
  }
  const types = shuffle(['rock', 'rock', 'rock', 'paper', 'paper', 'paper', 'scissors', 'scissors', 'scissors']);
  shuffle(cells);
  const pieces = types.map((type, index) => [cells[index][0], cells[index][1], type]);
  const candidates = [];
  for (let row = 3; row <= size - 4; row += 1) {
    for (let col = 0; col < size; col += 1) {
      if (row * size + col < (size - 1 - row) * size + (size - 1 - col)) candidates.push([row, col]);
    }
  }
  shuffle(candidates);
  let pairs = 2 + ((next() >>> 8) % 3);
  const wallsOf = (count) => candidates.slice(0, count).flatMap(([r, c]) => [[r, c], [size - 1 - r, size - 1 - c]]);
  let walls = wallsOf(pairs);
  while (walls.length && !openConnected(size, walls)) {
    pairs -= 1;
    walls = wallsOf(pairs);
  }
  return { size, pieces, walls };
}

export function layoutOf(map = 'standard') {
  if (typeof map === 'string') {
    if (/^random:[0-9a-f]{1,8}$/.test(map)) return { size: 9, pieces: randomFormation(map.slice(7)), walls: [] };
    if (/^chaos:[0-9a-f]{1,8}$/.test(map)) return chaosLayout(map.slice(6));
  }
  if (!Object.hasOwn(LAYOUTS, map)) throw new Error(`Bản đồ không tồn tại: ${map}`);
  return LAYOUTS[map];
}

export function formationOf(map = 'standard') {
  return layoutOf(map).pieces;
}

export function squareName(row, col, size = BOARD_SIZE) {
  return `${'abcdefghi'[col]}${size - row}`;
}

// Ô của Đỏ ứng với ô (row, col) của Xanh: quay 180° quanh tâm, hoặc lật qua đường chéo nối hai ô đích (`transpose`).
export function mirrorOf(row, col, size, mirror) {
  return mirror === 'transpose' ? [col, row] : [size - 1 - row, size - 1 - col];
}

export function makeBoard(map = 'standard') {
  const { size, pieces, walls, voids = [], mirror } = layoutOf(map);
  const board = Array.from({ length: size }, () => Array(size).fill(null));
  for (const [row, col, type] of pieces) {
    const [r, c] = mirrorOf(row, col, size, mirror);
    board[row][col] = { owner: 'p1', type };
    board[r][c] = { owner: 'p2', type };
  }
  for (const [row, col] of walls) board[row][col] = WALL;
  for (const [row, col] of voids) board[row][col] = VOID;
  return board;
}

function positionKey(board, turn) {
  return `${turn}:${board.map((line) => line.map((piece) =>
    isWall(piece) ? '#' : piece ? `${piece.owner}${piece.type[0]}` : '.').join('')).join('/')}`;
}

export function newGame(map = 'standard') {
  const board = makeBoard(map);
  return {
    board,
    turn: 'p1',
    winner: null,
    winReason: null,
    revision: 0,
    lastMove: null,
    history: [],
    quietPly: 0,
    repetitions: { [positionKey(board, 'p1')]: 1 },
  };
}

export function pieceCounts(board) {
  const counts = {
    p1: { rock: 0, paper: 0, scissors: 0 },
    p2: { rock: 0, paper: 0, scissors: 0 },
  };
  for (const line of board) {
    for (const piece of line) {
      if (piece && piece.owner) counts[piece.owner][piece.type] += 1;
    }
  }
  return counts;
}

export function legalMoves(board, row, col) {
  const source = board?.[row]?.[col];
  if (!source || !source.owner) return [];
  const result = [];
  for (let dr = -1; dr <= 1; dr += 1) {
    for (let dc = -1; dc <= 1; dc += 1) {
      if (dr === 0 && dc === 0) continue;
      const r = row + dr;
      const c = col + dc;
      if (r < 0 || r >= board.length || c < 0 || c >= board[r].length) continue;
      const target = board[r][c];
      if (!target) result.push({ row: r, col: c, kind: 'move' });
      else if (target.owner !== source.owner && BEATS[source.type] === target.type) {
        result.push({ row: r, col: c, kind: 'capture' });
      }
    }
  }
  return result;
}

// Số nước (8 hướng, đi vòng vật cản, bỏ qua quân) từ quân gần ô đích nhất của `side` tới ô đích của chính nó.
// Bàn không vật cản thì bằng khoảng cách Chebyshev quen thuộc.
export function nearestToGoal(board, side) {
  const size = board.length;
  const [goalRow, goalCol] = goalOf(side, size);
  const distance = Array.from({ length: size }, () => Array(size).fill(Infinity));
  distance[goalRow][goalCol] = 0;
  const queue = [[goalRow, goalCol]];
  for (let head = 0; head < queue.length; head += 1) {
    const [row, col] = queue[head];
    for (let dr = -1; dr <= 1; dr += 1) {
      for (let dc = -1; dc <= 1; dc += 1) {
        const r = row + dr;
        const c = col + dc;
        if (r < 0 || r >= size || c < 0 || c >= size || isWall(board[r][c]) || distance[r][c] !== Infinity) continue;
        distance[r][c] = distance[row][col] + 1;
        queue.push([r, c]);
      }
    }
  }
  let best = Infinity;
  for (let row = 0; row < size; row += 1) {
    for (let col = 0; col < size; col += 1) {
      if (board[row][col]?.owner === side) best = Math.min(best, distance[row][col]);
    }
  }
  return best;
}

// Xử ván khi hết thế mà chưa ai chạm đích hay xóa sổ một loại quân: bên còn NHIỀU QUÂN hơn thắng; bằng quân thì bên có
// quân GẦN Ô ĐÍCH hơn thắng; vẫn bằng thì hòa. `basis` cho biết căn cứ: material | advance | level.
export function adjudicate(game) {
  const counts = pieceCounts(game.board);
  const total = (side) => Object.values(counts[side]).reduce((a, b) => a + b, 0);
  if (total('p1') !== total('p2')) return { winner: total('p1') > total('p2') ? 'p1' : 'p2', basis: 'material' };
  const near1 = nearestToGoal(game.board, 'p1');
  const near2 = nearestToGoal(game.board, 'p2');
  if (near1 !== near2) return { winner: near1 < near2 ? 'p1' : 'p2', basis: 'advance' };
  return { winner: 'draw', basis: 'level' };
}

// Mã lý do của một ván được xử: `material:quiet`, `advance:repetition`...; hòa thật giữ nguyên `quiet`, `repetition`...
export function adjudicated(game, cause) {
  const verdict = adjudicate(game);
  return { winner: verdict.winner, reason: verdict.winner === 'draw' ? cause : `${verdict.basis}:${cause}` };
}

// options.adjudicate (Arena): hết thế thì xử thắng thay vì hòa. Mặc định giữ luật hòa cũ của trò chơi tay.
export function applyMove(game, side, from, to, options = {}) {
  if (game.winner) return { ok: false, error: 'Ván đấu đã kết thúc.' };
  if (game.turn !== side) return { ok: false, error: 'Chưa tới lượt của bạn.' };
  if (!Array.isArray(from) || !Array.isArray(to) ||
      from.length !== 2 || to.length !== 2 ||
      ![...from, ...to].every(Number.isInteger)) {
    return { ok: false, error: 'Tọa độ không hợp lệ.' };
  }
  const [fromRow, fromCol] = from;
  const [toRow, toCol] = to;
  const source = game.board[fromRow]?.[fromCol];
  if (!source || source.owner !== side) return { ok: false, error: 'Đó không phải quân của bạn.' };
  const legal = legalMoves(game.board, fromRow, fromCol).find((step) =>
    step.row === toRow && step.col === toCol);
  if (!legal) return { ok: false, error: 'Nước đi không hợp lệ.' };

  const captured = game.board[toRow][toCol];
  const board = game.board.map((line) => line.slice());
  board[fromRow][fromCol] = null;
  board[toRow][toCol] = source;
  const opponent = side === 'p1' ? 'p2' : 'p1';
  const counts = pieceCounts(board);
  const goal = goalOf(side, board.length);
  const reachedGoal = toRow === goal[0] && toCol === goal[1];
  let winner = null;
  let winReason = null;
  if (reachedGoal) {
    winner = side;
    winReason = 'goal';
  } else if (captured && counts[opponent][captured.type] === 0) {
    winner = side;
    winReason = `eliminate:${captured.type}`;
  }

  const nextTurn = side === 'p1' ? 'p2' : 'p1';
  const quietPly = captured ? 0 : game.quietPly + 1;
  // Sau khi ăn quân, thế cờ cũ không thể lặp lại với cùng số quân.
  const repetitions = captured ? {} : { ...game.repetitions };
  const key = positionKey(board, nextTurn);
  repetitions[key] = (repetitions[key] || 0) + 1;
  if (!winner && (repetitions[key] >= 3 || quietPly >= 80)) {
    const cause = repetitions[key] >= 3 ? 'repetition' : 'quiet';
    if (options.adjudicate) {
      ({ winner, reason: winReason } = adjudicated({ board }, cause));
    } else {
      winner = 'draw';
      winReason = cause;
    }
  }

  const lastMove = {
    side, from, to, type: source.type,
    captured: captured?.type ?? null,
    notation: `${squareName(fromRow, fromCol, board.length)}${captured ? '×' : '–'}${squareName(toRow, toCol, board.length)}`,
  };
  return {
    ok: true,
    state: {
      board,
      turn: winner ? side : nextTurn,
      winner,
      winReason,
      revision: game.revision + 1,
      lastMove,
      history: [...game.history, lastMove].slice(-120),
      quietPly,
      repetitions,
    },
  };
}
