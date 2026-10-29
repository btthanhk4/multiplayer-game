// Khán đài: xem trực tiếp (cập nhật tăng dần) và phát lại. Mỗi bàn là một `item`:
//   moves  : nước đi dạng gọn [hàng, cột, hàng mới, cột mới]
//   states : trạng thái bàn cờ sau từng nước (states[0] là thế ban đầu)
//   follow : đang bám theo nước mới nhất của ván trực tiếp
import {$, escape, api, notice, kinds, formatTime, badge, board, outcome, newGame, pieceCounts, teamLink, mapLabel,
  extendStates, compactMoves, pieceIcon, PIECE_NAMES} from './common.js';

// Live viewing is a luxury for the grader and the API, so each person watches at most two boards.
const MAX_WATCH = 2;
const LIVE = ['queued', 'running', 'failed'];
const SIDE = {p1: 'Xanh', p2: 'Đỏ'};
const EMPTY = '<div class="empty">Chọn một ván ở bên trái để theo dõi hoặc xem lại.<br>Đội bạn đang đấu? Ván sẽ hiện ở đầu danh sách.</div>';

export function createWatch(ctx) {
  const watched = new Map();
  let focused = null, liveTimer = 0, listed = '';

  const card = id => document.getElementById(`watch-${id}`);
  const last = item => item.states.length - 1;
  const clock = value => `${Number(value).toFixed(1)}s`;
  const count = () => { $('#watch-count').textContent = `${watched.size} / ${MAX_WATCH} BÀN`; };

  function material(state) {
    const counts = pieceCounts(state.board);
    const side = (who, label) => `<span class="side ${who}">${label} ${['rock', 'paper', 'scissors'].map(t => `<b class="${counts[who][t] <= 1 ? 'low' : ''}" title="${counts[who][t]} quân ${PIECE_NAMES[t]}">${pieceIcon(t)}${counts[who][t]}</b>`).join('')}</span>`;
    return `${side('p1', 'Xanh')}${side('p2', 'Đỏ')}`;
  }

  function strip(item) {
    const m = item.meta, l = item.live || {};
    if (item.offline) return '<span class="live-pill off">MẤT KẾT NỐI</span> Đang thử lại…';
    const viewers = item.viewers > 1 ? ` <span class="viewers">· ${item.viewers} người đang xem</span>` : '';
    if (item.status === 'queued') return `<span class="live-pill wait">ĐANG CHỜ</span> Máy chấm sẽ nhận ván này sớm${l.position ? ` · vị trí #${l.position}` : ''}${viewers}`;
    if (item.status === 'running' && item.liveOff) return `<span class="live-pill wait">ĐANG CHẠY</span> Xem trực tiếp đang tắt để ưu tiên chấm bài · bản phát lại sẽ có khi ván kết thúc${viewers}`;
    if (item.status === 'running') {
      const turn = l.thinking ? ` · <b>${SIDE[l.thinking]}</b> đang nghĩ` : ' · đang khởi động sandbox';
      const left = l.left ? ` · ⏱ Xanh ${clock(l.left.p1)} / Đỏ ${clock(l.left.p2)}` : '';
      return `<span class="live-pill">● TRỰC TIẾP</span> nước ${item.moves.length}${turn}${left}${viewers}`;
    }
    if (item.status === 'failed') return '<span class="live-pill wait">TẠM DỪNG</span> Ván gặp sự cố hạ tầng và sẽ tự chạy lại';
    if (item.status === 'cancelled') return '<span class="live-pill off">ĐÃ HỦY</span> Ván này đã bị ban tổ chức hủy';
    return `${badge('done')} ${escape(kinds[m.kind] || '')}${viewers}`;
  }

  function caption(item) {
    const m = item.meta, used = m.replay?.time_used;
    const parts = [`${m.bot1} (Xanh) / ${m.bot2} (Đỏ)`];
    if (m.map && m.map !== 'standard') parts.push(`Bản đồ ${mapLabel(m.map)}`);
    if (item.status === 'done' && used) parts.push(`nghĩ ${used.p1}s / ${used.p2}s`);
    if (m.replay?.execution_mode && m.replay.execution_mode !== 'docker') parts.push('Trận thử nội bộ');
    return `${item.status === 'done' ? `<strong>${escape(outcome(m))}</strong><br>` : ''}<span class="caption">${parts.map(escape).join(' · ')}</span>`;
  }

  function syncMoves(item, el) {
    const list = el.querySelector('[data-r=moves]');
    if (!list) return;
    if (item.listed !== item.moves.length) {
      let html = '';
      for (let n = 1; n <= item.moves.length; n += 2) {
        const a = item.states[n]?.lastMove?.notation, b = item.states[n + 1]?.lastMove?.notation;
        html += `<li><span class="n">${(n + 1) / 2}.</span><button type="button" data-jump="${item.id}" data-ply="${n}">${escape(a)}</button>${b ? `<button type="button" data-jump="${item.id}" data-ply="${n + 1}">${escape(b)}</button>` : '<span></span>'}</li>`;
      }
      list.innerHTML = html;
      el.querySelector('[data-r=count]').textContent = item.moves.length;
      item.listed = item.moves.length; item.active = null;
    }
    const button = list.querySelector(`button[data-ply="${item.position}"]`);
    if (item.active !== button) {
      item.active?.classList.remove('active');
      button?.classList.add('active');
      item.active = button;
      if (button) list.scrollTop = Math.max(0, button.offsetTop - list.clientHeight / 2);
    }
  }

  function paint(id) {
    const item = watched.get(id), el = card(id);
    if (!item?.meta || !el || !item.states) return;
    const end = last(item), at = Math.min(item.position, end), state = item.states[at], m = item.meta;
    const set = (name, html) => { const node = el.querySelector(`[data-r=${name}]`); if (node && node.dataset.h !== html) { node.innerHTML = html; node.dataset.h = html; } };
    set('strip', strip(item));
    set('material', material(state));
    const finished = item.status === 'done' && at === end;
    // Redraw the board only when the picture changes; one ply forward slides the moved piece instead of jumping.
    const shown = `${item.gen}|${at}|${item.flip}|${finished}|${item.status === 'cancelled'}`, wrap = el.querySelector('[data-r=board]');
    if (wrap.dataset.shown !== shown) {
      const animate = item.drawn?.at === at - 1 && item.drawn.flip === item.flip && item.drawn.gen === item.gen;
      wrap.style.setProperty('--move-ms', `${item.follow ? (at < end ? 120 : 220) : Math.min(260, Math.round(520 / item.speed))}ms`);
      wrap.innerHTML = board(state, {flip: item.flip, animate}) +
        (finished ? `<div class="result-banner ${m.winner}"><span class="result-mark" aria-hidden="true">${m.winner === 'draw' ? '🤝' : '🏆'}</span>${escape(outcome(m))}</div>` : item.status === 'cancelled' ? '<div class="result-banner">Ván đã bị hủy</div>' : '');
      wrap.dataset.shown = shown; item.drawn = {at, flip: item.flip, gen: item.gen};
    }
    set('caption', caption(item));
    const range = el.querySelector('[data-replay]');
    range.max = end; range.value = at; range.disabled = end === 0;
    el.querySelector('.replay-controls').hidden = end === 0 && item.status !== 'done';
    el.querySelector('[data-play]').textContent = item.playing ? 'Ⅱ' : '▷';
    el.querySelector('[data-speed]').textContent = `${item.speed}×`;
    el.querySelector('[data-golive]').hidden = !(item.status === 'running' && !item.follow);
    el.querySelector('[data-r=note]').textContent = `Nước ${at}/${end}${at ? ` · ${state.lastMove.notation}` : ' · Bắt đầu'}${at === end && item.status === 'running' ? ' · đang cập nhật' : ''}`;
    syncMoves(item, el);
  }

  function skeleton(id) {
    const item = watched.get(id), el = card(id), m = item?.meta;
    if (!item || !m || !el) return;
    el.classList.toggle('large', item.large);
    el.innerHTML = `<div class="watch-card-head"><strong>${teamLink(m.team1_id, m.team1)} <span class="red">×</span> ${teamLink(m.team2_id, m.team2)}${m.sibling ? ` <span class="game-no">${m.game_number > 2 ? 'Ván quyết định' : `Ván ${m.game_number}/2`}</span>` : ''}</strong><span class="watch-tools"><button class="text-link" data-flip="${id}" title="Xoay bàn cờ">Xoay</button><button class="text-link" data-share="${id}" title="Sao chép link ván này">Chia sẻ</button><button class="text-link" data-expand="${id}">${item.large ? 'Thu nhỏ' : 'Phóng to'}</button><button class="close-button" data-watch="${id}" aria-label="Bỏ xem trận">×</button></span></div>
      <div class="watch-card-body"><div class="live-strip" data-r="strip"></div><div class="material" data-r="material"></div><div class="board-wrap" data-r="board"></div><div class="replay-caption" data-r="caption"></div>
      <div class="replay-controls"><button data-step="-1" data-id="${id}" aria-label="Nước trước">‹</button><input type="range" min="0" max="0" value="0" data-replay="${id}" aria-label="Chọn nước trong ván"><button data-step="1" data-id="${id}" aria-label="Nước tiếp">›</button><button data-play="${id}" aria-label="Phát hoặc dừng">▷</button><button data-speed="${id}" aria-label="Tốc độ phát">1×</button><button class="golive" data-golive="${id}" hidden>● Trực tiếp</button></div>
      <p class="watch-note" data-r="note"></p>
      <details class="moves-box"${item.large || innerWidth >= 900 ? ' open' : ''}><summary>Nước đi (<span data-r="count">0</span>)</summary><ol class="move-list" data-r="moves"></ol><button class="text-link" data-copymoves="${id}">Sao chép danh sách nước đi</button></details>
      ${m.sibling ? `<div class="action-row"><button class="button" data-sibling="${id}">${m.game_number === 1 ? 'Xem ván 2' : 'Xem ván 1'} ↗</button></div>` : ''}</div>`;
    item.listed = -1; item.active = null; item.drawn = null;
    paint(id);
  }

  function setMoves(item, compact) {
    item.moves = compact.slice();
    item.gen = (item.gen || 0) + 1;   // a rebuilt move list may differ at the same ply, so the board must redraw
    item.states = [newGame(item.meta?.map)];
    extendStates(item.states, item.moves);
    item.position = Math.min(item.position, last(item));
  }

  async function load(id, first, opts = {}) {
    const data = await api(`/matches/${id}`);
    const item = watched.get(id);
    if (!item) return;
    const wasAtEnd = item.follow || item.states.length === 1 || item.position >= last(item);
    item.meta = data; item.status = data.status; item.viewers = data.viewers; item.offline = false;
    setMoves(item, data.replay ? compactMoves(data.replay.moves) : (data.progress?.moves || []));
    item.live = {thinking: data.progress?.thinking, left: data.progress?.left, position: data.position};
    const team = ctx.team();
    if (first) {
      item.flip = Boolean(team && data.team2_id === team.id && data.team1_id !== team.id);
      if (data.status === 'done') {
        const fromLink = Number.isInteger(opts.ply);
        item.position = fromLink ? Math.min(Math.max(opts.ply, 0), last(item)) : 0; item.playing = !fromLink; item.follow = false;
      } else { item.position = last(item); item.follow = data.status === 'running'; }
    } else if (data.status === 'done') {
      item.position = wasAtEnd ? last(item) : Math.min(item.position, last(item)); item.follow = false; item.playing = false;
    }
    skeleton(id);
  }

  function drop(id) {
    watched.delete(id);
    card(id)?.remove();
    if (focused === id) focused = null;
    if (!watched.size) $('#watch-grid').innerHTML = EMPTY;
    count();
  }

  async function pollLive(item) {
    let data;
    try { data = await api(`/matches/${item.id}/live?since=${item.moves.length}`); }
    catch (error) {
      if (error.status === 404) { drop(item.id); notice('Ván này không còn tồn tại.', true); return; }
      throw error;
    }
    const changed = data.status !== item.status || item.offline;
    item.offline = false; item.viewers = data.viewers; item.liveOff = data.live === false;
    item.live = {thinking: data.thinking, left: data.left, position: data.position};
    if (data.reset) setMoves(item, data.moves);
    else if (data.moves.length) { item.moves.push(...data.moves); extendStates(item.states, data.moves); }
    if (data.status === 'done') { await load(item.id, false); return; }
    item.status = item.meta.status = data.status;
    if (data.status === 'running' && item.states.length <= 1 && item.position === 0) item.follow = true;
    if (changed) skeleton(item.id); else paint(item.id);
  }

  function scheduleLive(delay = 2000) {
    clearTimeout(liveTimer);
    if (ctx.tab() !== 'watch' || document.hidden) return;
    if (![...watched.values()].some(item => LIVE.includes(item.status))) return;
    liveTimer = setTimeout(liveTick, delay * (0.85 + Math.random() * 0.3));
  }

  async function liveTick() {
    const items = [...watched.values()].filter(item => LIVE.includes(item.status));
    let worst = 0;
    await Promise.all(items.map(async item => {
      try { await pollLive(item); item.errors = 0; }
      catch (error) {
        item.errors = (item.errors || 0) + 1; worst = Math.max(worst, item.errors);
        if (!(error?.status >= 400 && error.status < 500 && error.status !== 429)) item.offline = true;
        if (watched.has(item.id) && item.meta) paint(item.id);
      }
    }));
    const quiet = items.some(item => item.liveOff);   // nothing new will arrive while live viewing is off
    scheduleLive(worst ? Math.min(15000, 2000 * 2 ** worst) : quiet ? 6000 : watched.size > 1 ? 3000 : 2000);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleLive(100); });

  // One tick moves the board; live games catch up to the newest move, replays play at the chosen speed.
  setInterval(() => {
    if (document.hidden || ctx.tab() !== 'watch') return;
    for (const [id, item] of watched) {
      if (!item.meta) continue;
      const end = last(item);
      if (item.follow) {
        if (item.position < end) { const behind = end - item.position; item.position = Math.min(end, item.position + (behind > 40 ? 8 : behind > 16 ? 4 : behind > 6 ? 2 : 1)); paint(id); }
        continue;
      }
      if (!item.playing) continue;
      item.clock += 130 * item.speed;
      if (item.clock < 650) continue;
      item.clock = 0;
      item.position = Math.min(item.position + 1, end);
      if (item.position === end) item.playing = false;
      paint(id);
    }
  }, 130);

  // ---- controls ----------------------------------------------------------
  function touch(id) {
    const item = watched.get(id);
    if (!item?.meta) return null;
    item.follow = false; item.playing = false;
    return item;
  }
  function jump(id, ply) { const item = touch(id); if (item) { item.position = Math.max(0, Math.min(last(item), ply)); paint(id); } }
  function step(id, delta) { const item = watched.get(id); if (item?.meta) jump(id, item.position + delta); }
  function play(id) {
    const item = watched.get(id);
    if (!item?.meta) return;
    if (item.follow) { item.follow = false; return paint(id); }
    if (item.position === last(item) && item.status === 'done') item.position = 0;
    item.playing = !item.playing; paint(id);
  }
  function goLive(id) { const item = watched.get(id); if (item?.status === 'running') { item.follow = true; item.playing = false; paint(id); } }

  async function copyText(text, label) {
    try { await navigator.clipboard.writeText(text); notice(`Đã sao chép ${label}.`); }
    catch { notice(text.length < 200 ? text : `Không sao chép được ${label}.`, true); }
  }

  function moveText(item) {
    const lines = [];
    for (let n = 1; n <= item.moves.length; n += 2) lines.push(`${(n + 1) / 2}. ${item.states[n].lastMove.notation}${item.states[n + 1] ? ` ${item.states[n + 1].lastMove.notation}` : ''}`);
    return lines.join('\n');
  }

  async function toggle(id, opts = {}) {
    if (watched.has(id)) { drop(id); return; }
    if (watched.size >= MAX_WATCH) throw new Error('Bạn đang xem hai bàn. Bỏ một bàn để mở trận khác.');
    if (ctx.tab() !== 'watch') ctx.setTab('watch');
    if (!watched.size) $('#watch-grid').replaceChildren();
    const item = {id, meta: null, moves: [], states: [newGame()], position: 0, playing: false, speed: 1, clock: 0, follow: false,
                  flip: false, large: false, status: 'loading', live: null, viewers: 0, errors: 0, listed: -1, active: null};
    watched.set(id, item);
    const el = document.createElement('article');
    el.className = 'watch-card'; el.id = `watch-${id}`; el.textContent = 'Đang tải bàn cờ…';
    $('#watch-grid').append(el);
    count();
    try { await load(id, true, opts); }
    catch (error) { drop(id); throw error; }
    scheduleLive(800);
  }

  async function replace(from, to) { drop(from); await toggle(to); }

  // ---- directory ---------------------------------------------------------
  function row(m) {
    const live = m.status === 'running';
    const meta = `${m.pairing_id ? `Giải · vòng ${m.round}` : kinds[m.kind] || 'Trận tập'} · ${formatTime(m.created)}${m.status === 'done' ? ` · ${outcome(m)}` : ''}`;
    return `<article class="match-row${live ? ' is-live' : ''}">${live ? '<span class="live-pill">● TRỰC TIẾP</span>' : badge(m.status)}<p><strong>${teamLink(m.team1_id, m.team1)}</strong> · ${escape(m.bot1)}<br>× <strong>${teamLink(m.team2_id, m.team2)}</strong> · ${escape(m.bot2)}</p><p class="caption">${escape(meta)}</p><button class="button" data-watch="${m.id}">${watched.has(m.id) ? 'Bỏ xem' : live ? 'Xem trực tiếp' : m.status === 'done' ? 'Xem replay' : 'Theo dõi'}</button></article>`;
  }

  async function directory() {
    const filter = $('#match-filter').value, kind = $('#match-kind').value, query = $('#match-search').value.trim();
    let html;
    if (filter === 'mine' && !ctx.team()) html = '<div class="caption">Lập đội để xem các ván của đội. Chọn “Tất cả” để xem đội khác.</div>';
    else {
      let items;
      if (filter === 'mine') {
        items = (await api('/matches?mine=1')).items.filter(m => (!kind || m.kind === kind) && (!query || `${m.team1} ${m.team2}`.toLowerCase().includes(query.toLowerCase())));
      } else {
        const params = new URLSearchParams();
        if (['running', 'queued', 'done'].includes(filter)) params.set('status', filter);
        if (kind) params.set('kind', kind);
        if (query) params.set('q', query);
        items = (await api(`/matches?${params}`)).items;
      }
      html = items.length ? items.map(row).join('') : '<div class="caption">Chưa có ván phù hợp.</div>';
    }
    if (html !== listed) { $('#match-list').innerHTML = html; listed = html; }
    count();
  }

  async function randomLive() {
    const {items} = await api('/matches?status=running');
    const open = items.filter(m => !watched.has(m.id));
    if (!open.length) { notice(items.length ? 'Bạn đang xem hết các trận đang đấu.' : 'Hiện chưa có trận nào đang đấu. Thử lại sau ít giây.'); return; }
    await toggle(open[Math.floor(Math.random() * open.length)].id);
  }

  // ---- events ------------------------------------------------------------
  function onClick(button) {
    const d = button.dataset;
    if (d.jump) jump(d.jump, Number(d.ply));
    else if (d.step && d.id) step(d.id, Number(d.step));
    else if (d.play) play(d.play);
    else if (d.golive) goLive(d.golive);
    else if (d.speed) { const item = watched.get(d.speed); if (item) { item.speed = {1: 2, 2: 4, 4: 1}[item.speed]; paint(d.speed); } }
    else if (d.expand) { const item = watched.get(d.expand); if (item) { item.large = !item.large; skeleton(d.expand); } }
    else if (d.flip) { const item = watched.get(d.flip); if (item) { item.flip = !item.flip; paint(d.flip); } }
    else if (d.share) {
      const item = watched.get(d.share), ply = item && item.status === 'done' && item.position > 0 && item.position < last(item) ? `&ply=${item.position}` : '';
      copyText(`${location.origin}/#watch=${d.share}${ply}`, 'link ván đấu');
    } else if (d.copymoves) { const item = watched.get(d.copymoves); if (item) copyText(moveText(item), 'danh sách nước đi'); }
    else if (d.sibling) { const item = watched.get(d.sibling); if (item?.meta?.sibling) replace(d.sibling, item.meta.sibling).catch(error => notice(error.message, true)); }
  }

  function onInput(event) {
    const id = event.target.dataset?.replay;
    if (!id) return;
    const item = touch(id);
    if (item) { item.position = Number(event.target.value); paint(id); }
  }

  function onKey(event) {
    if (ctx.tab() !== 'watch' || !watched.size || event.target.closest('input,textarea,select,dialog') || event.ctrlKey || event.metaKey || event.altKey) return;
    if (watched.size > 1 && !focused && event.target.closest('button')) return;
    const id = focused && watched.has(focused) ? focused : [...watched.keys()][0], item = watched.get(id);
    if (!item?.meta) return;
    const key = event.key;
    if (key === 'ArrowLeft' || key === 'ArrowRight') { event.preventDefault(); step(id, (key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 10 : 1)); }
    else if (key === 'Home') { event.preventDefault(); jump(id, 0); }
    else if (key === 'End') { event.preventDefault(); item.status === 'running' ? goLive(id) : jump(id, last(item)); }
    else if (key === ' ' && !event.target.closest('button')) { event.preventDefault(); play(id); }
    else if (key === 'f' || key === 'F') { item.flip = !item.flip; paint(id); }
    else if (key === 'l' || key === 'L') goLive(id);
  }

  $('#watch-grid').addEventListener('pointerdown', event => {
    const el = event.target.closest('.watch-card');
    if (!el) return;
    focused = el.id.slice(6);
    for (const other of document.querySelectorAll('.watch-card')) other.classList.toggle('focused', watched.size > 1 && other === el);
  });

  return {
    toggle, replace, directory, randomLive, onClick, onInput, onKey, scheduleLive,
    has: id => watched.has(id), size: () => watched.size,
    clear() { for (const id of [...watched.keys()]) drop(id); },
  };
}
