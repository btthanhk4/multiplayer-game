import {newGame, applyMove, squareName, pieceCounts} from '../rules.js';

export const $ = selector => document.querySelector(selector);
export const $$ = selector => [...document.querySelectorAll(selector)];
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const labels = {queued:'Đang chờ',running:'Đang chạy',valid:'Hợp lệ',rejected:'Cần sửa',failed:'Chờ xử lý sự cố',
  done:'Đã kết thúc',open:'Đang nhận đăng ký',frozen:'Đã khóa bot',completed:'Hoàn tất',cancelled:'Đã hủy',
  active:'Đang thi',invalid:'Bot không hợp lệ',disqualified:'Truất quyền'};
export const formats = {swiss:'Swiss theo điểm',round_robin:'Vòng tròn',knockout:'Loại trực tiếp'};
export const kinds = {practice:'Trận tập',ranked:'Xếp hạng',tournament:'Giải đấu',campaign:'Chiến dịch',friendly:'Giao hữu'};
export const formatTime = seconds => seconds ? new Date(seconds*1000).toLocaleString('vi-VN', {dateStyle:'short',timeStyle:'short'}) : '—';
export const badge = status => `<span class="badge ${['rejected','failed','disqualified','invalid'].includes(status)?'bad':['queued','frozen','running'].includes(status)?'wait':''}">${escape(labels[status] || status)}</span>`;

export const session = {csrf:null};

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, data, options = {}) {
  const init = {credentials:'same-origin', headers:{}};
  if (options.form) {
    init.method = 'POST'; init.body = options.form; init.headers['X-CSRF-Token'] = session.csrf || '';
  } else if (data !== undefined) {
    init.method = 'POST'; init.body = JSON.stringify(data);
    init.headers = {'Content-Type':'application/json','X-CSRF-Token':session.csrf || ''};
  }
  let response;
  try { response = await fetch(`/api${path}`, init); }
  catch { throw new ApiError('Mất kết nối tới máy chủ. Kiểm tra mạng rồi thử lại.', 0); }
  if (options.raw) {
    if (!response.ok) throw new ApiError((await response.json().catch(() => ({}))).error || 'Không tải được.', response.status);
    return response;
  }
  let result;
  try { result = await response.json(); }
  catch { throw new ApiError(response.status >= 500 ? 'Máy chủ đang quá tải. Hệ thống sẽ thử lại tự động.' : 'Chưa kết nối được máy chủ đấu trường.', response.status); }
  if (!response.ok) throw new ApiError(result.error || 'Không thực hiện được yêu cầu.', response.status);
  return result;
}

let noticeTimer;
export function notice(message, error = false) {
  const el = $('#notice');
  el.hidden = false; el.classList.toggle('error', error); el.textContent = message;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, error ? 12000 : 6000);
}

export async function action(button, work) {
  if (button) button.disabled = true;
  try { await work(); } catch (error) { notice(error.message, true); }
  finally { if (button) button.disabled = false; }
}

export function options(select, items, placeholder = 'Chưa có bot hợp lệ', label = item => item.name) {
  const el = typeof select === 'string' ? $(select) : select, before = el.value;
  el.innerHTML = items.length ? items.map(item => `<option value="${escape(item.id)}">${escape(label(item))}</option>`).join('') : `<option value="">${escape(placeholder)}</option>`;
  if (items.some(item => item.id === before)) el.value = before;
}

export function countdown(deadline) {
  const left = Math.round(deadline - Date.now()/1000);
  if (left <= 0) return 'đã hết hạn';
  const d = Math.floor(left/86400), h = Math.floor(left%86400/3600), m = Math.floor(left%3600/60);
  return d ? `còn ${d} ngày ${h} giờ` : h ? `còn ${h} giờ ${m} phút` : `còn ${Math.max(m,1)} phút`;
}

export function duration(seconds) {
  if (seconds == null) return '—';
  if (seconds < 1) return '< 1 giây';
  if (seconds < 90) return `${Math.round(seconds)} giây`;
  if (seconds < 5400) return `${Math.round(seconds/60)} phút`;
  return `${(seconds/3600).toFixed(1)} giờ`;
}

// Poll with jitter so 1,000 open tabs do not hit the server in lock-step, pause
// while hidden, and back off when the server answers 429/5xx or is unreachable.
export function poller(work, interval) {
  let timer, failures = 0;
  const schedule = () => {
    clearTimeout(timer);
    const base = (typeof interval === 'function' ? interval() : interval) * 2 ** Math.min(failures, 3);
    timer = setTimeout(run, base * (0.75 + Math.random()*0.5));
  };
  const run = async () => {
    if (document.hidden) return schedule();
    try { await work(); failures = 0; }
    catch (error) { failures += 1; if (!(error instanceof ApiError)) console.error(error); }
    schedule();
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) run(); });
  return {now: run, schedule};
}

export function board(state, {flip = false} = {}) {
  // Flipping turns the board 180 degrees so the viewer's own side is at the bottom.
  const order = flip ? [8,7,6,5,4,3,2,1,0] : [0,1,2,3,4,5,6,7,8];
  return `<div class="mini-board${flip ? ' flipped' : ''}" role="img" aria-label="Bàn cờ 9 nhân 9, ${state.revision} nước">${order.flatMap((r, i) => order.map((c, j) => {
    const p = state.board[r][c];
    const goal=r===0&&c===8?'goal-blue':r===8&&c===0?'goal-red':'';
    const last=state.lastMove&&[state.lastMove.from,state.lastMove.to].some(x=>x[0]===r&&x[1]===c);
    const labels=`${j===0&&!goal?`<small class="rank">${9-r}</small>`:''}${i===8?`<small class="file">${'abcdefghi'[c]}</small>`:''}`;
    return `<div class="square ${(r+c)%2?'dark':''} ${goal} ${last?'last':''}" title="${squareName(r,c)}${p?` · ${p.owner==='p1'?'Xanh':'Đỏ'} · ${{rock:'Đấm',paper:'Lá',scissors:'Kéo'}[p.type]}`:''}">${p?`<span class="stone ${p.owner}">${{rock:'Đ',paper:'L',scissors:'K'}[p.type]}</span>`:goal?`<small>${squareName(r,c)}</small>`:''}${labels}</div>`;
  })).join('')}</div>`;
}

// Moves travel as [row, col, row, col]; the side is whoever is to move in the previous state.
export function extendStates(states, compact) {
  for (const [fr, fc, tr, tc] of compact) {
    const previous = states.at(-1), next = applyMove(previous, previous.turn, [fr, fc], [tr, tc]);
    if (!next.ok) throw new Error('Replay không khớp phiên bản luật. Hãy liên hệ BTC.');
    states.push(next.state);
  }
}
export const compactMoves = moves => moves.map(m => [...m.from, ...m.to]);

const reasons = {goal:'Chạm ô đích',quiet:'80 nửa lượt không ăn quân',repetition:'Lặp thế ba lần',
  no_legal_moves:'Không còn nước hợp lệ',ply_limit:'Đủ giới hạn nửa lượt','technical:both':'Cả hai bot lỗi'};
export function outcome(match) {
  if (match.status !== 'done') return match.status === 'queued' && match.position ? `Đang chờ · vị trí #${match.position}` : labels[match.status] || match.status;
  const who = match.winner === 'draw' ? 'Ván hòa' : `${match.winner === 'p1' ? 'Xanh' : 'Đỏ'} thắng`;
  const reason = reasons[match.reason] || (match.reason?.startsWith('technical:') ? 'Đối thủ lỗi thực thi/nước đi'
    : match.reason?.startsWith('eliminate:') ? 'Loại hết một loại quân' : match.reason);
  return `${who} · ${reason}`;
}

export function replayStates(moves) {
  const states = [newGame()];
  for (const move of moves) {
    const next = applyMove(states.at(-1), move.side, move.from, move.to);
    if (!next.ok) throw new Error('Replay không khớp phiên bản luật. Hãy liên hệ BTC.');
    states.push(next.state);
  }
  return states;
}

export {newGame, pieceCounts};

// Team names open the public profile wherever they appear.
export const teamLink = (id, name) => id ? `<button type="button" class="team-link" data-team="${escape(id)}">${escape(name)}</button>` : escape(name);

export const starText = count => `${'★'.repeat(count)}${'☆'.repeat(3 - count)}`;

export function ago(seconds, skew = 0) {
  const left = Math.max(0, Date.now()/1000 + skew - seconds);
  if (left < 60) return 'vừa xong';
  if (left < 3600) return `${Math.floor(left/60)} phút trước`;
  if (left < 86400) return `${Math.floor(left/3600)} giờ trước`;
  return formatTime(seconds);
}

export function sparkline(points, width = 320, height = 72) {
  if (points.length < 2) return '';
  const values = points.map(p => p.rating), low = Math.min(...values), high = Math.max(...values), span = Math.max(high - low, 20);
  const x = i => (i * (width - 8) / (points.length - 1) + 4).toFixed(1);
  const y = v => (height - 6 - (v - low) * (height - 12) / span).toFixed(1);
  const line = values.map((v, i) => `${x(i)},${y(v)}`).join(' ');
  const last = values.length - 1;
  return `<svg class="sparkline" viewBox="0 0 ${width} ${height}" role="img" aria-label="Elo từ ${values[0]} đến ${values[last]}, cao nhất ${high}"><polyline class="area" points="4,${height} ${line} ${x(last)},${height}"/><polyline class="trend" points="${line}"/><circle cx="${x(last)}" cy="${y(values[last])}" r="3.5"/></svg>`;
}

let celebrateTimer;
export function celebrate(message) {
  notice(message);
  const host = $('#celebrate');
  if (!host || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  host.replaceChildren();
  const banner = document.createElement('div');
  banner.className = 'celebrate-banner'; banner.textContent = message; host.append(banner);
  for (let i = 0; i < 40; i += 1) {
    const piece = document.createElement('span');
    piece.className = `confetti ${i % 2 ? 'p2' : 'p1'}`; piece.textContent = 'ĐLK'[i % 3];
    piece.style.setProperty('--x', `${Math.random()*100}vw`);
    piece.style.setProperty('--fall', `${1.8 + Math.random()*1.6}s`);
    piece.style.setProperty('--spin', `${Math.round(Math.random()*720 - 360)}deg`);
    piece.style.setProperty('--wait', `${(Math.random()*0.6).toFixed(2)}s`);
    host.append(piece);
  }
  host.classList.add('on');
  clearTimeout(celebrateTimer);
  celebrateTimer = setTimeout(() => { host.classList.remove('on'); host.replaceChildren(); }, 4200);
}
