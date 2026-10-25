export const BOARD_SIZE = 9;
export const TYPES = ['rock', 'paper', 'scissors'];
export const TYPE_LABEL = { rock: 'Đấm', paper: 'Lá', scissors: 'Kéo' };
export const SIDE_LABEL = { p1: 'Xanh', p2: 'Đỏ' };
export const BEATS = { rock: 'scissors', paper: 'rock', scissors: 'paper' };
export const GOALS = { p1: [0, 8], p2: [8, 0] };

// Quân Xanh xuất phát quanh a1; quân Đỏ quay 180° quanh tâm bàn cờ.
// Mỗi bên có đúng ba quân của từng loại. Hai ô đích lúc đầu để trống.
// "Bản đồ" chỉ đổi cách xếp quân ban đầu: luật đi, ô đích và kích thước bàn cờ không đổi. Mọi cách xếp nằm ở
// hàng 6–8 của Xanh (Đỏ đối xứng ở hàng 0–2), nên hai bên luôn cách nhau ít nhất 4 hàng và không có ăn quân ngay.
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
export const MAP_NAMES = ['standard', 'wall', 'flank', 'block'];

// `random:<1-8 chữ số hex>`: cách xếp ngẫu nhiên nhưng xác định theo seed (cùng seed → cùng bàn cờ ở mọi nơi).
// Dùng bộ sinh LCG 32 bit và lấy các bit cao để cùng kết quả với bản Python của sdk/local_arena.py.
export function randomFormation(seedHex) {
  let state = parseInt(seedHex, 16) >>> 0;
  const next = () => (state = (Math.imul(state, 1664525) + 1013904223) >>> 0);
  const shuffle = (list) => {
    for (let i = list.length - 1; i > 0; i -= 1) {
      const j = (next() >>> 8) % (i + 1);
      [list[i], list[j]] = [list[j], list[i]];
    }
    return list;
  };
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

export function formationOf(map = 'standard') {
  if (typeof map === 'string' && /^random:[0-9a-f]{1,8}$/.test(map)) return randomFormation(map.slice(7));
  if (!Object.hasOwn(FORMATIONS, map)) throw new Error(`Bản đồ không tồn tại: ${map}`);
  return FORMATIONS[map];
}

export function squareName(row, col) {
  return `${'abcdefghi'[col]}${BOARD_SIZE - row}`;
}

export function makeBoard(map = 'standard') {
  const board = Array.from({ length: BOARD_SIZE }, () => Array(BOARD_SIZE).fill(null));
  for (const [row, col, type] of formationOf(map)) {
    board[row][col] = { owner: 'p1', type };
    board[8 - row][8 - col] = { owner: 'p2', type };
  }
  return board;
}

function positionKey(board, turn) {
  return `${turn}:${board.map((line) => line.map((piece) =>
    piece ? `${piece.owner}${piece.type[0]}` : '.').join('')).join('/')}`;
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
      if (piece) counts[piece.owner][piece.type] += 1;
    }
  }
  return counts;
}

export function legalMoves(board, row, col) {
  const source = board?.[row]?.[col];
  if (!source) return [];
  const result = [];
  for (let dr = -1; dr <= 1; dr += 1) {
    for (let dc = -1; dc <= 1; dc += 1) {
      if (dr === 0 && dc === 0) continue;
      const r = row + dr;
      const c = col + dc;
      if (r < 0 || r >= BOARD_SIZE || c < 0 || c >= BOARD_SIZE) continue;
      const target = board[r][c];
      if (!target) result.push({ row: r, col: c, kind: 'move' });
      else if (target.owner !== source.owner && BEATS[source.type] === target.type) {
        result.push({ row: r, col: c, kind: 'capture' });
      }
    }
  }
  return result;
}

// Khoảng cách (8 hướng) từ quân gần ô đích nhất của `side` tới ô đích của chính nó.
function nearestToGoal(board, side) {
  const [goalRow, goalCol] = GOALS[side];
  let best = Infinity;
  for (let row = 0; row < BOARD_SIZE; row += 1) {
    for (let col = 0; col < BOARD_SIZE; col += 1) {
      if (board[row][col]?.owner === side) best = Math.min(best, Math.max(Math.abs(row - goalRow), Math.abs(col - goalCol)));
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
  const reachedGoal = toRow === GOALS[side][0] && toCol === GOALS[side][1];
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
    notation: `${squareName(fromRow, fromCol)}${captured ? '×' : '–'}${squareName(toRow, toCol)}`,
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
