import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {explainBotError} from '../arena/explain.js';
import {FIELDS, PRESETS, buildBot, readValues, clamp} from '../arena/builder.js';

const template = readFileSync(new URL('../sdk/examples/custom.py', import.meta.url), 'utf8');

test('lỗi bot được giải thích bằng tiếng Việt và nêu đúng dòng', () => {
  const syntax = explainBotError('Không nạp được bot: SyntaxError: invalid syntax (<string>, line 12)');
  assert.match(syntax.title, /cú pháp/);
  assert.match(syntax.hint, /dòng 12/);
  assert.match(explainBotError('Không nạp được bot: IndentationError: unexpected indent (<string>, line 3)').title, /Thụt lề/);
  assert.match(explainBotError("Không nạp được bot: ModuleNotFoundError: No module named 'numpy'").title, /numpy/);
  assert.match(explainBotError("Bot lỗi thực thi: NameError: name 'dist' is not defined").title, /dist/);
  assert.match(explainBotError('Bot vượt giới hạn 3 giây/lượt.').title, /quá lâu/);
  assert.match(explainBotError('Bot trả nước đi không hợp lệ.').hint, /legalMoves/);
});

test('lỗi định dạng kết quả được ưu tiên hơn tên lỗi Python đi kèm', () => {
  assert.match(explainBotError('Kết quả không phải JSON: TypeError: Object of type set is not JSON serializable').title, /định dạng/);
  assert.match(explainBotError('Bot lỗi thực thi: TypeError: unsupported operand').title, /TypeError/);
});

test('lỗi lạ vẫn có lời khuyên chung và không có lỗi thì không có gì để nói', () => {
  assert.ok(explainBotError('một lỗi chưa ai thấy').hint);
  assert.equal(explainBotError(''), null);
  assert.equal(explainBotError(undefined), null);
});

test('mẫu bot tùy chỉnh đọc được và ghi lại đúng bốn con số', () => {
  assert.deepEqual(readValues(template), PRESETS[0].values);
  for (const preset of PRESETS) {
    const source = buildBot(template, preset.values);
    assert.deepEqual(readValues(source), preset.values, preset.id);
    assert.equal(source.split('\n').length, template.split('\n').length, 'chỉ đổi giá trị, không thêm bớt dòng');
    assert.ok(source.includes('def choose_move(observation)'));
  }
});

test('thanh trượt bị ép vào khoảng cho phép, giá trị lạ không phá mã', () => {
  const wild = buildBot(template, {CHASE_GOAL: -5, CAPTURE: 9999, SAFETY: 'abc', RANDOMNESS: 3.6});
  assert.deepEqual(readValues(wild), {CHASE_GOAL: 0, CAPTURE: 30, SAFETY: 0, RANDOMNESS: 4});
  assert.doesNotMatch(wild, /NaN|undefined/);
  for (const field of FIELDS) assert.equal(clamp(field, 10 ** 9), field.max);
});

test('mẫu thiếu dòng cấu hình thì báo lỗi thay vì lặng lẽ bỏ qua thanh trượt', () => {
  assert.throws(() => buildBot('def choose_move(o):\n    pass\n', PRESETS[0].values), /CHASE_GOAL/);
});

test('mỗi tính cách có sẵn nằm trong giới hạn và có tên khác nhau', () => {
  assert.equal(new Set(PRESETS.map(p => p.id)).size, PRESETS.length);
  for (const preset of PRESETS) for (const field of FIELDS) {
    assert.equal(clamp(field, preset.values[field.key]), preset.values[field.key], `${preset.id}.${field.key}`);
  }
});

test('bàn cờ vẽ quân bằng biểu tượng và chỉ trượt quân khi được yêu cầu', async () => {
  const {board, newGame, pieceIcon} = await import('../arena/common.js');
  const {applyMove} = await import('../rules.js');
  const start = newGame();
  const html = board(start);
  assert.doesNotMatch(html, /class="stone[^"]*">[ĐLK]</, 'không còn quân chữ cái');
  assert.equal((html.match(/<svg class="glyph"/g) || []).length, 18, 'mỗi quân một biểu tượng');
  for (const type of ['rock', 'paper', 'scissors']) assert.match(pieceIcon(type), /<path d="M/);
  const moved = applyMove(start, 'p1', [6, 0], [5, 0]).state;
  assert.doesNotMatch(board(moved), /slide/, 'không có hiệu ứng nếu không yêu cầu');
  assert.match(board(moved, {animate: true}), /stone p1 \w+ slide fx0 fy1"/, 'quân Xanh đi lên một ô: trượt từ ô bên dưới');
  assert.match(board(moved, {animate: true, flip: true}), /slide fx0 fy-1"/, 'bàn xoay thì hướng trượt đảo lại');
  assert.equal((board(moved, {animate: true}).match(/ghost/g) || []).length, 0, 'không ăn quân thì không có quân mờ');
});

test('ăn quân để lại quân bị ăn mờ dần đúng màu và loại', async () => {
  const {board} = await import('../arena/common.js');
  const state = {revision: 5, board: Array.from({length: 9}, () => Array(9).fill(null)),
    lastMove: {side: 'p2', from: [3, 3], to: [4, 4], type: 'scissors', captured: 'paper'}};
  state.board[4][4] = {owner: 'p2', type: 'scissors'};
  const html = board(state, {animate: true});
  assert.match(html, /stone p1 paper ghost/);
  assert.match(html, /stone p2 scissors slide fx-1 fy-1/);
  assert.match(html, /class="square [^"]*last to"/);
  assert.match(html, /class="square [^"]*last from"/);
});

test('ô đích không bị chồng nhãn tọa độ', async () => {
  const {board, newGame} = await import('../arena/common.js');
  const html = board(newGame());
  const goal = html.match(/<div class="square [^"]*goal-red[^"]*"[^>]*>(.*?)<\/div>/)[1];
  assert.doesNotMatch(goal, /class="(rank|file)"/);
});
