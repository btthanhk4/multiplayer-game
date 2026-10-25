import {$, $$, escape, labels, formats, kinds, formatTime, badge, session, api, notice, action, options, countdown,
  poller, board, outcome, replayStates, newGame, pieceCounts, teamLink, starText, ago, sparkline, celebrate} from './common.js';

let me = null, team = null, health = null, tab = 'overview', register = false;
let tournaments = [], submissions = [], selectedTournament = '', competition = null, standingsLimit = 100, ranked = null;
let campaign = null, badges = null, selectedLevel = null, profile = null;
const watched = new Map();
const DRAFT = 'ottv2-draft';
const TABS = ['overview','team','lab','campaign','ranked','competition','watch'];
const CLEAR = 1.5;
const TIER_CLASS = {'Đồng':'bronze','Bạc':'silver','Vàng':'gold','Kim cương':'diamond'};
const tier = label => `<span class="tier ${TIER_CLASS[label] || ''}">${escape(label)}</span>`;

function storage(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
}

// Slow-moving panels (feed, badges, campaign on the dashboard) are fetched at most
// every few polls so a thousand open dashboards stay cheap for the server.
let memo = {};
function cached(key, ms, load) {
  const hit = memo[key];
  if (hit && Date.now() - hit.at < ms) return hit.value;
  const value = load();
  memo[key] = {at: Date.now(), value};
  value.catch(() => { if (memo[key]?.value === value) delete memo[key]; });
  return value;
}
const forget = () => { memo = {}; };

function setTab(value) {
  if (!TABS.includes(value)) value = 'overview';
  tab = value;
  $$('.view').forEach(el => el.hidden = el.id !== tab);
  $$('[role=tab]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.tab === tab)));
  history.replaceState(null, '', `#${tab}`);
  refresher.now();
}

function requireLogin() {
  if (me) return true;
  openAuth(false);
  return false;
}

function requireTeam() {
  if (!requireLogin()) return false;
  if (team) return true;
  notice('Bạn cần tạo đội hoặc vào đội bằng mã trước.', true);
  setTab('team');
  return false;
}

function openAuth(asRegister) {
  register = !asRegister;
  $('#toggle-auth').click();
  $('#auth-dialog').showModal();
}

async function loadMe() {
  try {
    const result = await api('/me');
    me = result.user; team = result.team; session.csrf = result.csrf;
  } catch { me = team = null; }
  renderAccount();
}

function renderAccount() {
  $('#account-button').textContent = me ? `${me.name}${team ? ` · ${team.name}` : ''}` : 'Đăng nhập';
  $('#admin-link').hidden = me?.role !== 'admin';
  $('#password-banner').hidden = !me?.must_change;
  $('#hero').hidden = Boolean(me);
  $('#hero-actions').hidden = Boolean(me);
}

const skew = () => health ? health.server_time - Date.now()/1000 : 0;

function renderHealth() {
  $('#service-status').textContent = health.open.read_only ? 'Đang bảo trì · chỉ xem' : health.runner_online ? `Máy chấm đang chạy · ${health.runner_slots} luồng` : 'Bài nộp được lưu · Chờ máy chấm';
  $('#service-status').classList.toggle('online', health.runner_online && !health.open.read_only);
  $('#service-detail').textContent = health.runner_online && health.runner_modes.some(m => m !== 'docker') ? 'Đang thử nội bộ · Chưa chấm giải chính thức' : `${health.pending} lượt chấm đang chờ hoặc đang chạy`;
  $('#announcement').hidden = !health.announcement;
  $('#announcement').textContent = health.announcement;
  $('#invite-field').hidden = !health.registration_invite;
  $('#team-caption').textContent = `Mỗi đội tối đa ${health.team_size} thành viên. Bot, điểm Elo, huy hiệu và đăng ký giải thuộc về đội.`;
}

// ---- notifications ------------------------------------------------------

// Remember what this browser has already announced; the first load only records.
function fresh(key, ids) {
  let known = null;
  try { known = JSON.parse(storage(key) || 'null'); } catch {}
  storage(key, JSON.stringify([...new Set([...(known || []), ...ids])].slice(-200)));
  return known ? ids.filter(id => !known.includes(id)) : [];
}

function announceBadges(items) {
  if (!team) return;
  const earned = items.filter(b => b.earned);
  const added = new Set(fresh(`ottv2-badges-${team.id}`, earned.map(b => b.key)));
  const news = earned.filter(b => added.has(b.key));
  if (news.length) celebrate(`Huy hiệu mới: ${news.map(b => `${b.icon} ${b.title}`).join(' · ')}`);
}

function announceRuns(mine) {
  if (!team || !mine) return;
  const done = mine.runs.filter(r => r.status === 'done');
  const added = new Set(fresh(`ottv2-runs-${team.id}`, done.map(r => r.id)));
  for (const run of done.filter(r => added.has(r.id))) {
    const points = String(run.points).replace('.', ',');
    if (run.stars) celebrate(`Hạ Boss ${run.level} · ${run.boss}! ${starText(run.stars)}`);
    else notice(`Chưa qua ải ${run.level} (${points}/2 điểm). Xem replay để tìm điểm yếu rồi thử lại!`);
  }
}

function announceSeries(mine) {
  if (!team || !mine) return;
  const done = mine.series.filter(s => s.status === 'done');
  const added = new Set(fresh(`ottv2-series-${team.id}`, done.map(s => s.id)));
  for (const s of done.filter(item => added.has(item.id))) {
    const youAreA = s.a === team.id, points = youAreA ? s.a_points : 2 - s.a_points;
    const delta = youAreA ? s.rating_change : -s.rating_change, opponent = youAreA ? s.b_name : s.a_name;
    const text = `${points > 1 ? 'Thắng' : points === 1 ? 'Hòa' : 'Thua'} ${opponent} ${String(points).replace('.', ',')}–${String(2 - points).replace('.', ',')} · ${delta >= 0 ? '+' : ''}${delta} Elo`;
    if (points > 1) celebrate(text); else notice(text);
  }
}

// ---- dashboard ----------------------------------------------------------

function openTournaments() {
  return tournaments.filter(t => t.status === 'open' && t.deadline > Date.now()/1000 && !t.seed_from);
}

function renderDashboard(recent) {
  const valid = submissions.filter(s => s.status === 'valid');
  const latest = submissions[0];
  const entered = new Set((team?.entries || []).map(e => e.tournament_id));
  const cleared = campaign?.mine?.cleared || 0, levels = campaign?.bosses?.length || 5;
  const steps = [
    ['Tạo tài khoản', Boolean(me), () => openAuth(true)],
    ['Lập hoặc vào đội', Boolean(team), () => setTab('team')],
    ['Có bot hợp lệ', valid.length > 0, () => setTab('lab')],
    ['Hạ boss đầu tiên', cleared > 0, () => setTab('campaign')],
    ['Đăng ký giải', entered.size > 0, () => setTab('competition')],
    ['Xem ván đầu tiên', recent.some(m => m.status === 'done'), () => setTab('watch')],
  ];
  const next = steps.findIndex(step => !step[1]);
  $('#checklist').hidden = false;
  $('#checklist').innerHTML = `<p class="eyebrow">LỘ TRÌNH CỦA BẠN · ${steps.filter(s => s[1]).length}/${steps.length}</p><ol>${steps.map(([label, done], index) =>
    `<li class="${done ? 'done' : index === next ? 'next' : ''}"><span>${done ? '✓' : String(index+1).padStart(2,'0')}</span><button class="text-link" data-step="${index}">${escape(label)}</button></li>`).join('')}</ol>`;
  $('#checklist').onclick = event => { const b = event.target.closest('[data-step]'); if (b) steps[Number(b.dataset.step)][2](); };
  const open = openTournaments()[0];
  const cards = [];
  cards.push(team ? `<article class="dash-card"><span class="eyebrow">ĐỘI</span><h3>${escape(team.name)}</h3><p>${team.members.length} thành viên · mã <strong>${escape(team.code)}</strong></p><button class="button" data-tab="team">Quản lý đội ↗</button></article>`
    : `<article class="dash-card highlight"><span class="eyebrow">ĐỘI</span><h3>Chưa có đội</h3><p>Tạo đội mới hoặc nhập mã đội bạn bè gửi.</p><button class="button primary" data-tab="team">Lập đội ↗</button></article>`);
  cards.push(latest ? `<article class="dash-card"><span class="eyebrow">BOT MỚI NHẤT</span><h3>${escape(latest.name)}</h3><p>${badge(latest.status)} ${latest.position ? `vị trí #${latest.position} trong hàng chờ` : escape((latest.log || '').length > 90 ? `${latest.log.slice(0, 88)}…` : latest.log || '')}</p><button class="button" data-tab="lab">Mở xưởng bot ↗</button></article>`
    : `<article class="dash-card ${team ? 'highlight' : ''}"><span class="eyebrow">BOT</span><h3>Chưa nộp bot</h3><p>Bắt đầu từ bot mẫu, nộp và chờ kết quả kiểm tra.</p><button class="button ${team ? 'primary' : ''}" data-tab="lab">Viết bot ↗</button></article>`);
  const nextBoss = campaign?.bosses?.[Math.min(cleared, levels - 1)];
  cards.push(`<article class="dash-card ${valid.length && cleared < levels ? 'highlight' : ''}"><span class="eyebrow">CHIẾN DỊCH BOSS</span><h3>Ải ${cleared}/${levels} · ${campaign?.mine?.stars || 0} ★</h3><p>${cleared >= levels ? 'Đội đã hạ trùm cuối. Thử lấy đủ 3 sao mọi ải!' : nextBoss ? `Tiếp theo: Boss ${nextBoss.level} · ${escape(nextBoss.name)}` : 'Đánh bại 5 boss mạnh dần.'}</p><button class="button ${valid.length && cleared < levels ? 'primary' : ''}" data-tab="campaign">${cleared ? 'Leo tiếp ↗' : 'Vào ải đầu ↗'}</button></article>`);
  const mine = ranked?.mine;
  cards.push(`<article class="dash-card"><span class="eyebrow">ELO XẾP HẠNG</span><h3>${mine ? `${mine.rating} · ${escape(mine.tier)}` : '1000 · Bạc'}</h3><p>${mine?.position ? `Hạng #${mine.position} · ${mine.played} cặp` : 'Chưa có cặp trận xếp hạng.'}</p><button class="button" data-tab="ranked">Tìm đối thủ ↗</button></article>`);
  cards.push(open ? `<article class="dash-card ${entered.has(open.id) ? '' : 'highlight'}"><span class="eyebrow">GIẢI ĐANG MỞ · ${escape(countdown(open.deadline).toUpperCase())}</span><h3>${escape(open.name)}</h3><p>${entered.has(open.id) ? 'Đội đã đăng ký. Có thể đổi bot trước hạn.' : `Hạn chọn bot: ${formatTime(open.deadline)}`}</p><button class="button ${entered.has(open.id) ? '' : 'primary'}" data-tournament="${open.id}">${entered.has(open.id) ? 'Xem giải ↗' : 'Đăng ký ngay ↗'}</button></article>`
    : `<article class="dash-card"><span class="eyebrow">GIẢI ĐẤU</span><h3>Chưa có giải mở đăng ký</h3><p>Leo tháp boss và đấu xếp hạng trong lúc chờ ban tổ chức.</p><button class="button" data-tab="competition">Xem các giải ↗</button></article>`);
  $('#dashboard-cards').innerHTML = cards.join('');
  $('#recent-matches').innerHTML = recent.length ? recent.slice(0, 8).map(matchRow).join('')
    : `<div class="empty">${me ? 'Đội chưa có ván nào. Vào ải boss đầu tiên hoặc tìm đối thủ xếp hạng để bắt đầu.' : 'Đăng nhập để xem các ván của đội.'}</div>`;
  renderBadgeSummary();
}

const TAGS = {upset:['hot','LẬT KÈO'], sweep:['','THẮNG TRẮNG'], final:['hot','TRÙM CUỐI'], champion:['gold','VÔ ĐỊCH']};

function renderFeed(data) {
  const s = data.stats;
  $('#arena-stats').innerHTML = [['Đội đã có bot', s.teams_ready], ['Ván trong 24 giờ', s.games_today], ['Đang đấu trực tiếp', s.live], ['Lượt hạ boss', s.boss_clears]]
    .map(([label, value]) => `<div class="arena-stat"><strong>${Number(value).toLocaleString('vi-VN')}</strong><span>${label}</span></div>`).join('');
  $('#feed').innerHTML = data.events.length ? data.events.slice(0, 12).map(e => {
    const tags = e.tags.map(tag => `<span class="tag ${TAGS[tag]?.[0] || ''}">${TAGS[tag]?.[1] || tag}</span>`).join('');
    const text = e.kind === 'ranked' ? `${teamLink(e.winner, e.winner_name)} thắng ${teamLink(e.loser, e.loser_name)} <strong>${e.points === 2 ? '2–0' : '1,5–0,5'}</strong> · +${e.change} Elo`
      : e.kind === 'boss' ? `${teamLink(e.winner, e.winner_name)} hạ <strong>Boss ${e.level} · ${escape(e.boss || '')}</strong> <span class="stars">${starText(e.stars)}</span>`
      : `🏆 ${teamLink(e.winner, e.winner_name)} vô địch <strong>${escape(e.tournament_name)}</strong>`;
    const mine = team && [e.winner, e.loser].includes(team.id);
    return `<li class="${mine ? 'mine' : ''}"><span class="feed-icon ${e.kind}" aria-hidden="true">${e.kind === 'boss' ? 'BOSS' : e.kind === 'champion' ? 'CÚP' : 'ELO'}</span><p>${text} ${tags}<br><small>${ago(e.time, skew())}</small></p>${e.match ? `<button class="text-link" data-watch="${e.match}">Xem ↗</button>` : e.tournament ? `<button class="text-link" data-tournament="${e.tournament}">Bảng điểm ↗</button>` : ''}</li>`;
  }).join('') : '<li class="caption">Chưa có diễn biến. Cặp xếp hạng và lượt hạ boss đầu tiên sẽ hiện ở đây.</li>';
}

function badgeTiles(items) {
  return items.map(b => `<div class="achievement ${b.earned ? 'earned' : 'locked'}"><span class="achievement-icon" aria-hidden="true">${b.icon}</span><div><strong>${escape(b.title)}</strong><small>${b.earned ? '' : 'Chưa mở · '}${escape(b.detail)}</small></div></div>`).join('');
}

function renderBadgeSummary() {
  if (!team || !badges) {
    $('#badge-summary').innerHTML = `<p class="caption">${me ? 'Lập đội để bắt đầu sưu tập huy hiệu.' : 'Đăng nhập và lập đội để bắt đầu sưu tập huy hiệu.'}</p>`;
    return;
  }
  const earned = badges.filter(b => b.earned), goal = badges.find(b => !b.earned);
  $('#badge-summary').innerHTML = `<p class="badge-score"><strong>${earned.length}</strong> / ${badges.length} huy hiệu</p><div class="badge-row">${earned.map(b => `<span title="${escape(b.title)}">${b.icon}</span>`).join('') || '<span class="caption">Chưa có. Nộp bot đầu tiên để mở 🐣</span>'}</div>${goal ? `<p class="caption">Mục tiêu kế tiếp: <strong>${goal.icon} ${escape(goal.title)}</strong> — ${escape(goal.detail)}.</p>` : '<p class="caption">Đội đã sưu tập đủ bộ. Huyền thoại!</p>'}`;
}

function tournamentCards() {
  $('#tournament-count').textContent = `${tournaments.length} GIẢI ĐẤU`;
  const entered = new Set((team?.entries || []).map(e => e.tournament_id));
  $('#tournament-list').innerHTML = tournaments.length ? tournaments.map(t => `<article class="tournament-card"><div class="card-top"><span class="eyebrow">${escape(formats[t.format] || t.format).toUpperCase()}</span>${badge(t.status)}</div><h3>${escape(t.name)}</h3><p class="caption">${t.entrants} đội · Vòng ${t.round}/${t.rounds}${t.status === 'open' ? `<br>Hạn chọn bot: ${formatTime(t.deadline)} (${countdown(t.deadline)})` : ''}${entered.has(t.id) ? '<br><strong>Đội bạn đã đăng ký</strong>' : ''}</p><button class="button" data-tournament="${t.id}">Xem giải ↗</button></article>`).join('')
    : '<div class="empty">Giải khai màn đang được chuẩn bị.<br>Bạn có thể lập đội, viết bot, leo tháp boss và đấu xếp hạng trước.</div>';
  options('#competition-select', tournaments, 'Chưa có giải đấu');
  if (!tournaments.some(t => t.id === selectedTournament)) selectedTournament = (openTournaments()[0] || tournaments[0])?.id || '';
  $('#competition-select').value = selectedTournament;
}

// ---- team ---------------------------------------------------------------

function renderTeam() {
  $('#team-none').hidden = !me || Boolean(team);
  $('#team-detail').hidden = !team;
  $('#team-badges-card').hidden = !team;
  if (!me) { $('#team-none').hidden = true; return; }
  if (!team) return;
  const owner = team.owner === me.id;
  $('#team-name').textContent = team.name;
  $('#team-status').textContent = team.status === 'active' ? (owner ? 'Bạn là trưởng nhóm.' : 'Bạn là thành viên.') : 'Đội đã bị truất quyền thi đấu. Liên hệ ban tổ chức.';
  $('#team-code').textContent = team.code;
  $$('.owner-only').forEach(el => el.hidden = !owner);
  $('#member-count').textContent = `${team.members.length} / ${health?.team_size || 4} thành viên`;
  $('#member-list').innerHTML = team.members.map(m => `<li><span><strong>${escape(m.name)}</strong><small>@${escape(m.username)}${m.id === team.owner ? ' · trưởng nhóm' : ''}</small></span>${owner && m.id !== me.id ? `<button class="text-link danger" data-kick="${m.id}">Mời ra</button>` : ''}</li>`).join('');
  if (badges) {
    $('#badge-count').textContent = `${badges.filter(b => b.earned).length} / ${badges.length} ĐÃ MỞ`;
    $('#team-badges').innerHTML = badgeTiles([...badges].sort((a, b) => b.earned - a.earned));
  }
}

// ---- lab ----------------------------------------------------------------

function submissionRows() {
  $('#submission-count').textContent = `${submissions.length} PHIÊN BẢN`;
  $('#submission-list').innerHTML = !me ? '<div class="empty">Đăng nhập và lập đội để lưu chiến thuật.</div>'
    : !team ? '<div class="empty">Lập đội hoặc vào đội để nộp bot.</div>'
    : submissions.length ? submissions.map(s => `<article class="submission"><div><strong>${escape(s.name)}</strong><small>${formatTime(s.created)} · ${escape(s.author || (s.origin === 'form' ? 'Google Form' : ''))} · ${escape(s.hash.slice(0,10))}</small></div>${badge(s.status)}<div class="submission-log">${s.position ? `Vị trí #${s.position} trong hàng chờ kiểm tra.` : escape(s.log || 'Bài đã được lưu. Chờ máy chấm tiếp nhận.')}</div><div class="submission-actions"><button class="text-link" data-source="${s.id}">Mở mã ↗</button>${s.status==='valid'?`<button class="text-link" data-boss-bot="${s.id}">Đánh boss ↗</button><button class="text-link" data-rank-bot="${s.id}">Tìm đối thủ ↗</button>`:''}</div></article>`).join('')
    : '<div class="empty">Chưa có bài nộp. Bắt đầu bằng một bot mẫu ở phía trên.</div>';
  const valid = submissions.filter(s => s.status === 'valid');
  for (const id of ['#practice-a','#practice-b','#ranked-submission','#campaign-submission']) options(id, valid);
  options('#join-submission', submissions.filter(s => ['valid','queued','running'].includes(s.status)), 'Chưa có bot hợp lệ',
    s => s.status === 'valid' ? s.name : `${s.name} (đang kiểm tra)`);
}

function updateSize() {
  $('#source-size').textContent = `${new TextEncoder().encode($('#source').value).length.toLocaleString('vi-VN')} B`;
  storage(DRAFT, JSON.stringify({source: $('#source').value, name: $('#bot-name').value}));
}

// ---- campaign -----------------------------------------------------------

const starsFor = points => points == null || points < CLEAR ? 0 : points >= 2 ? 3 : 2;

function renderCampaign(data) {
  campaign = data;
  const mine = data.mine, best = mine?.best || {}, cleared = mine?.cleared || 0, levels = data.bosses.length;
  $('#campaign-summary').textContent = mine ? `Ải ${cleared}/${levels} · ${mine.stars}/${levels*3} ★` : me ? 'Lập đội để leo tháp' : 'Đăng nhập để leo tháp';
  const open = level => level <= cleared + 1;
  if (!selectedLevel || !open(selectedLevel)) selectedLevel = Math.min(cleared + 1, levels);
  $('#boss-path').innerHTML = data.bosses.length ? data.bosses.map(b => {
    const stars = starsFor(best[b.level]), state = stars ? 'cleared' : open(b.level) ? 'open' : 'locked';
    return `<li class="boss ${state} ${selectedLevel === b.level ? 'selected' : ''}"><span class="boss-level">${String(b.level).padStart(2,'0')}</span><div class="boss-body"><h3>${escape(b.name)}${b.level === levels ? ' <span class="tag hot">TRÙM CUỐI</span>' : ''}</h3><p>${escape(b.blurb)}</p><span class="stars" aria-label="${stars} trên 3 sao">${starText(stars)}</span>${best[b.level] != null ? `<small class="caption"> · tốt nhất ${String(best[b.level]).replace('.', ',')}/2</small>` : ''}${b.status !== 'valid' ? ' <span class="badge wait">Boss đang khởi động</span>' : ''}</div><button class="button ${state === 'open' ? 'primary' : ''}" data-boss="${b.level}" ${state === 'locked' ? 'disabled' : ''}>${state === 'locked' ? 'Đang khóa' : state === 'cleared' ? 'Đấu lại' : 'Chọn ải'}</button></li>`;
  }).join('') : '<li class="empty">Ban tổ chức chưa bật các boss.</li>';
  const boss = data.bosses.find(b => b.level === selectedLevel);
  const running = mine?.runs.find(r => r.status === 'running');
  $('#campaign-target').textContent = boss ? `Ải ${boss.level} · ${boss.name}` : 'Chọn một ải đã mở';
  $('#campaign-level').value = boss?.level || '';
  const closed = !health?.open.campaign_open;
  $('#campaign-go').disabled = Boolean(running) || closed || !boss || boss.status !== 'valid';
  const position = running?.positions?.filter(Boolean)[0];
  $('#campaign-status').textContent = closed ? 'Chiến dịch đang tạm đóng để ưu tiên giải đấu.'
    : !me ? 'Đăng nhập và lập đội để thách đấu.' : !team ? 'Lập đội để thách đấu.'
    : running ? ([running.first_status, running.second_status].includes('failed') ? 'Một ván gặp sự cố hạ tầng; ban tổ chức sẽ chạy lại.'
      : `Đang đấu Boss ${running.level} · ${running.boss}. ${position ? `Ván đang ở vị trí #${position} trong hàng chờ.` : 'Hai ván đang chạy trên server.'}`)
    : boss?.status !== 'valid' ? 'Boss đang được máy chấm kiểm tra, chờ ít phút.'
    : 'Hai ván đổi màu với boss. Được từ 1,5 điểm là qua ải.';
  $('#campaign-runs').innerHTML = mine?.runs.length ? mine.runs.map(r => `<div class="campaign-run"><p><strong>Ải ${r.level} · ${escape(r.boss || '')}</strong><br><span class="caption">${escape(r.bot)} · ${r.status === 'done' ? `${String(r.points).replace('.', ',')}/2 điểm · ${r.stars ? `<span class="stars">${starText(r.stars)}</span>` : 'chưa qua'}` : escape(labels[r.status] || r.status)}</span></p><div class="action-row"><button class="text-link" data-watch="${r.first_match}">Ván 1</button><button class="text-link" data-watch="${r.second_match}">Ván 2</button></div></div>`).join('')
    : '<p class="caption">Chưa có lượt thách đấu nào.</p>';
  $('#campaign-climbers').textContent = `${data.climbers} ĐỘI ĐÃ QUA ÍT NHẤT MỘT ẢI · ${data.finishers} ĐỘI HẠ TRÙM CUỐI`;
  $('#campaign-hall').innerHTML = data.hall.length ? data.hall.map((h, i) => `<tr class="${h.team_id === team?.id ? 'mine' : ''}"><td class="rank">${i + 1}</td><td>${teamLink(h.team_id, h.name)}</td><td><strong>${h.cleared}/${levels}</strong></td><td><span class="stars">${'★'.repeat(h.stars)}</span> ${h.stars}</td><td>${formatTime(h.reached_at)}</td></tr>`).join('')
    : '<tr><td colspan="5">Chưa đội nào qua ải. Trở thành người đầu tiên!</td></tr>';
  announceRuns(mine);
}

// ---- ranked -------------------------------------------------------------

function renderRanked(data) {
  ranked = data;
  $('#ranked-waiting').textContent = `${data.waiting} đội đang tìm trận`;
  const mine = data.mine;
  $('#ranked-mine').textContent = mine ? `${mine.rating} Elo · ${mine.tier}${mine.position ? ` · hạng #${mine.position}` : ''}` : me ? 'Lập đội để có điểm Elo.' : 'Đăng nhập để xem điểm của đội.';
  const active = mine?.series.find(series => series.status === 'running');
  $('#ranked-form').hidden = Boolean(mine?.queued || active) || !health?.open.ranked_open;
  $('#ranked-cancel').hidden = !mine?.queued;
  const position = active?.positions?.filter(Boolean)[0];
  $('#ranked-queue').textContent = !health?.open.ranked_open ? 'Đấu xếp hạng đang tạm đóng để ưu tiên giải đấu.'
    : mine?.queued ? `Đang tìm đối thủ cho ${mine.queued.bot}. Có thể rời trang; bot vẫn ở hàng chờ.`
    : active ? [active.first_status,active.second_status].includes('failed') ? 'Một ván gặp sự cố hạ tầng. Ban tổ chức sẽ chạy lại; chưa tính điểm.'
      : `Đã ghép với ${active.a===team.id?active.b_name:active.a_name}. ${position ? `Ván đang ở vị trí #${position} trong hàng chờ.` : 'Hai ván đang chạy trên server.'}`
    : 'Chọn bot hợp lệ để tìm một đội khác.';
  $('#ranked-standings').innerHTML = data.leaderboard.length ? data.leaderboard.map((row,index) =>
    `<tr class="${row.team_id===team?.id?'mine':''}"><td class="rank">${index < 3 ? ['🥇','🥈','🥉'][index] : index+1}</td><td>${teamLink(row.team_id, row.name)}</td><td><strong>${row.rating}</strong></td><td>${tier(row.tier)}</td><td>${row.played}</td><td>${row.wins} / ${row.draws} / ${row.losses}</td></tr>`).join('')
    : '<tr><td colspan="6">Chưa có cặp trận xếp hạng hoàn thành.</td></tr>';
  const series = mine?.series || [];
  $('#ranked-series').innerHTML = series.length ? series.map(s => {
    const youAreA = s.a === team.id, opponent = youAreA ? s.b_name : s.a_name, opponentId = youAreA ? s.b : s.a;
    const points = s.a_points == null ? '—' : youAreA ? s.a_points : 2 - s.a_points;
    const delta = s.rating_change == null ? '' : (youAreA ? s.rating_change : -s.rating_change);
    return `<article class="series-card ${points !== '—' ? points > 1 ? 'won' : points < 1 ? 'lost' : '' : ''}"><div><span class="eyebrow">${s.status==='done'?'ĐÃ KẾT THÚC':s.status==='cancelled'?'ĐÃ HỦY':'ĐANG XỬ LÝ'}</span><h3>Đội bạn × ${teamLink(opponentId, opponent)}</h3><p>${escape(youAreA?s.a_bot:s.b_bot)} / ${escape(youAreA?s.b_bot:s.a_bot)}</p><strong>${String(points).replace('.', ',')} / 2 điểm ván${delta!==''?` · ${delta>=0?'+':''}${delta} Elo`:''}</strong></div><div class="series-actions"><button class="button" data-watch="${s.first_match}">Xem ván 1</button><button class="button" data-watch="${s.second_match}">Xem ván 2</button></div></article>`;
  }).join('') : '<div class="empty">Chưa có cặp trận. Chọn bot hợp lệ để tìm đối thủ đầu tiên.</div>';
  announceSeries(mine);
}

// ---- competition --------------------------------------------------------

async function renderCompetition() {
  if (!selectedTournament) {
    $('#competition-info').innerHTML = '<div class="empty">Chưa có giải đấu.</div>';
    $('#join-form').hidden = true; $('#standings-body').innerHTML = ''; $('#round-view').hidden = true; return;
  }
  const id = selectedTournament, result = await api(`/tournaments/${id}`);
  if (id !== selectedTournament) return;
  competition = result;
  const t = result.tournament;
  $('#competition-info').innerHTML = `<div class="tournament-head"><p>${badge(t.status)} <strong>${escape(t.name)}</strong></p><p class="caption">${escape(formats[t.format])} · Vòng ${t.round}/${t.rounds}${result.unfinished ? ` · ${result.unfinished} ván đang chờ/chạy` : ''}${t.status === 'open' ? ` · Hạn chọn bot ${formatTime(t.deadline)} (${countdown(t.deadline)})` : ''}<br>Luật cố định: ${escape(t.rules_hash.slice(0,12))} · ${t.config.turn_seconds}s/lượt · quỹ ${t.config.game_seconds}s/ván · Hai ván đổi màu mỗi cặp.</p>${t.description ? `<p>${escape(t.description)}</p>` : ''}</div>${podium(result)}`;
  const open = t.status === 'open' && t.deadline > Date.now()/1000 && !t.seed_from;
  $('#join-form').hidden = !open || !me;
  const mine = result.mine;
  $('#withdraw').hidden = !mine || !open;
  $('#entry-status').textContent = !team ? 'Lập đội để đăng ký.'
    : mine ? `Đội đã chọn: ${mine.bot}${mine.bot_status !== 'valid' ? ' (đang chờ kiểm tra — cần hợp lệ trước khi BTC khóa danh sách)' : ''}. Có thể đổi trước hạn.`
    : 'Chọn một phiên bản để giữ suất của đội.';
  const roundSelect = $('#round-select'), before = roundSelect.value;
  roundSelect.innerHTML = `<option value="">Bảng điểm tổng</option>${Array.from({length:t.round}, (_, i) => `<option value="${i+1}">Vòng ${i+1}</option>`).join('')}`;
  if ([...roundSelect.options].some(o => o.value === before)) roundSelect.value = before;
  renderStandings();
  await renderRound();
}

function podium(result) {
  if (result.tournament.status !== 'completed') return '';
  const top = result.standings.filter(s => s.rank && s.rank <= 3);
  if (!top.length) return '';
  return `<div class="podium" aria-label="Bục trao giải">${[2, 1, 3].map(rank => top.filter(s => s.rank === rank)).map((rows, i) => rows.length ? `<div class="step step-${[2, 1, 3][i]}"><span>${['🥈','🥇','🥉'][i]}</span>${rows.map(s => `<strong>${teamLink(s.team_id, s.name)}</strong>`).join('')}<small>Hạng ${[2, 1, 3][i]}</small></div>` : '').join('')}</div>`;
}

function renderStandings() {
  if (!competition) return;
  const query = $('#standings-search').value.trim().toLowerCase();
  const rows = competition.standings.filter(s => !query || s.name.toLowerCase().includes(query) || s.bot.toLowerCase().includes(query));
  const ko = competition.tournament.format === 'knockout';
  $('#standings-body').innerHTML = rows.length ? rows.slice(0, standingsLimit).map(s => `<tr class="${s.team_id===team?.id?'mine':''}"><td class="rank">${s.rank ?? '—'}</td><td>${teamLink(s.team_id, s.name)}<small>${escape(s.bot)}${s.seed ? ` · hạt giống ${s.seed}` : ''}</small></td><td>${escape(s.tier)}</td><td><strong>${ko ? (s.eliminated ? `Dừng ở vòng ${s.eliminated}` : s.reached ? `Đang ở vòng ${s.reached}` : '—') : s.points}</strong></td><td>${s.wins} / ${s.draws} / ${s.losses}${s.byes?` · ${s.byes} BYE`:''}</td><td>${s.buchholz}</td><td>${s.game_points}</td></tr>`).join('')
    : '<tr><td colspan="7">Chưa có đội đăng ký.</td></tr>';
  $('#standings-more').hidden = rows.length <= standingsLimit;
}

async function renderRound() {
  const number = $('#round-select').value;
  $('#round-view').hidden = !number;
  if (!number) return;
  const {items} = await api(`/tournaments/${selectedTournament}/rounds/${number}`);
  $('#round-view').innerHTML = items.length ? items.map(p => {
    const result = !p.b ? 'BYE' : p.forfeit ? `${p.forfeit === p.a ? escape(p.b_name) : escape(p.a_name)} thắng do đối thủ bị truất quyền`
      : p.games.map((g, i) => `<button class="text-link" data-watch="${g.id}">Ván ${i+1}: ${escape(outcome(g))}</button>`).join(' · ');
    return `<div class="pairing ${[p.a,p.b].includes(team?.id)?'mine':''}"><strong>${teamLink(p.a, p.a_name)}</strong> <span class="red">×</span> <strong>${p.b ? teamLink(p.b, p.b_name) : '—'}</strong><p>${result}</p></div>`;
  }).join('') : '<div class="empty">Vòng này chưa có cặp đấu.</div>';
}

// ---- team profiles ------------------------------------------------------

async function openTeam(id) {
  const data = await api(`/teams/${id}`);
  profile = data;
  const own = team?.id === data.id, record = data.record;
  const valid = submissions.filter(s => s.status === 'valid');
  const invite = !data.system && !own && team && data.bot && health?.open.practice_open;
  $('#team-profile').innerHTML = `<h2>${escape(data.name)}</h2><p class="caption">${data.system ? 'Đội boss của ban tổ chức trong chiến dịch.' : `${data.members} thành viên · lập lúc ${formatTime(data.created)}${data.bot ? ` · bot đại diện: ${escape(data.bot)}` : ''}`}</p>
    ${data.system ? '' : `<div class="profile-stats"><div><span>ELO</span><strong>${data.rating}</strong><small>${tier(data.tier)}${data.position ? ` · hạng #${data.position}` : ''}</small></div><div><span>ĐỈNH</span><strong>${data.peak}</strong><small>${record.played} cặp xếp hạng</small></div><div><span>T / H / B</span><strong>${record.wins}/${record.draws}/${record.losses}</strong><small>Theo cặp</small></div><div><span>CHIẾN DỊCH</span><strong>${data.campaign.cleared}/${data.campaign.levels}</strong><small>${data.campaign.stars} ★</small></div></div>
    ${sparkline(data.history) || '<p class="caption">Chưa có lịch sử Elo.</p>'}
    <p class="eyebrow">HUY HIỆU · ${data.badges.filter(b => b.earned).length}/${data.badges.length}</p><div class="badge-row large">${data.badges.filter(b => b.earned).map(b => `<span title="${escape(b.title)}: ${escape(b.detail)}">${b.icon}</span>`).join('') || '<span class="caption">Chưa có huy hiệu.</span>'}</div>`}
    ${invite ? `<form id="friendly-form" class="friendly-form"><p class="eyebrow">MỜI GIAO HỮU · 2 VÁN ĐỔI MÀU · KHÔNG TÍNH ELO</p>${valid.length ? `<label>Bot của đội bạn<select name="submission" required>${valid.map(s => `<option value="${escape(s.id)}">${escape(s.name)}</option>`).join('')}</select></label><button class="button primary">Thách đấu ${escape(data.name)} ↗</button>` : '<p class="caption">Đội bạn cần một bot hợp lệ để giao hữu.</p>'}</form>` : ''}
    <button class="text-link" type="button" data-copy-team="${escape(data.id)}">Sao chép link hồ sơ ↗</button>`;
  if (!$('#team-dialog').open) $('#team-dialog').showModal();
}

// ---- watch --------------------------------------------------------------

function matchRow(m) {
  return `<article class="match-row">${badge(m.status)}<p><strong>${teamLink(m.team1_id, m.team1)}</strong> · ${escape(m.bot1)}<br>× <strong>${teamLink(m.team2_id, m.team2)}</strong> · ${escape(m.bot2)}</p><p class="caption">${m.pairing_id ? `Giải · vòng ${m.round}` : escape(kinds[m.kind] || 'Trận tập')} · ${formatTime(m.created)}${m.status === 'done' ? ` · ${escape(outcome(m))}` : ''}</p><button class="button" data-watch="${m.id}">${watched.has(m.id) ? 'Bỏ xem' : m.status === 'done' ? 'Xem replay' : 'Theo dõi'}</button></article>`;
}

async function matchDirectory() {
  const filter = $('#match-filter').value;
  if (filter === 'mine' && !team) {
    $('#match-list').innerHTML = '<div class="caption">Lập đội để xem các ván của đội. Chọn “Tất cả” để xem đội khác.</div>';
  } else {
    const {items} = await api(filter === 'mine' ? '/matches?mine=1' : filter === 'all' ? '/matches' : `/matches?status=${filter}`);
    $('#match-list').innerHTML = items.length ? items.map(matchRow).join('') : '<div class="caption">Chưa có ván phù hợp.</div>';
  }
  $('#watch-count').textContent = `${watched.size} / 4 BÀN`;
}

const SHORT = {rock:'Đ', paper:'L', scissors:'K'};
function material(state) {
  const counts = pieceCounts(state.board);
  const side = (who, label) => `<span class="side ${who}">${label} ${['rock','paper','scissors'].map(t => `<b class="${counts[who][t] <= 1 ? 'low' : ''}" title="${counts[who][t]} quân ${{rock:'Đấm',paper:'Lá',scissors:'Kéo'}[t]}">${SHORT[t]}${counts[who][t]}</b>`).join('')}</span>`;
  return `<div class="material">${side('p1', 'Xanh')}${side('p2', 'Đỏ')}</div>`;
}

function renderWatch(id) {
  const item = watched.get(id), el = document.getElementById(`watch-${id}`);
  if (!item?.data || !el) return;
  const m = item.data, complete = m.status === 'done';
  const state = complete ? item.states[item.position] : m.progress?.state || newGame();
  const end = complete && item.position === item.states.length - 1;
  el.classList.toggle('large', Boolean(item.large));
  el.innerHTML = `<div class="watch-card-head"><strong>${teamLink(m.team1_id, m.team1)} <span class="red">×</span> ${teamLink(m.team2_id, m.team2)}</strong><span class="watch-tools"><button class="text-link" data-share="${id}" title="Sao chép link ván này">Chia sẻ</button><button class="text-link" data-expand="${id}">${item.large ? 'Thu nhỏ' : 'Phóng to'}</button><button class="close-button" data-watch="${id}" aria-label="Bỏ xem trận">×</button></span></div><div class="watch-card-body">${material(state)}<div class="board-wrap">${board(state)}${end ? `<div class="result-banner ${m.winner}">${escape(outcome(m))}</div>` : ''}</div><div class="replay-caption">${escape(outcome(m))}<br><span class="caption">${escape(kinds[m.kind] || '')} · ${escape(m.bot1)} (Xanh) / ${escape(m.bot2)} (Đỏ) · ${state.revision} nước${complete && m.replay?.time_used ? ` · nghĩ ${m.replay.time_used.p1}s / ${m.replay.time_used.p2}s` : ''}</span></div>${complete?`<div class="replay-controls"><button data-step="-1" data-id="${id}" aria-label="Nước trước">‹</button><input type="range" min="0" max="${item.states.length-1}" value="${item.position}" data-replay="${id}" aria-label="Chọn nước trong replay"><button data-step="1" data-id="${id}" aria-label="Nước tiếp">›</button><button data-play="${id}" aria-label="Phát hoặc dừng replay">${item.playing?'Ⅱ':'▷'}</button><button data-speed="${id}" aria-label="Tốc độ phát">${item.speed}×</button></div><p class="watch-note">Nước ${item.position}/${item.states.length-1}${item.position?` · ${escape(m.replay.moves[item.position-1].notation)}`:' · Bắt đầu'}${m.replay.execution_mode!=='docker'?' · Trận thử nội bộ':''}${watched.size === 1 ? ' · Phím ← → và Space để tua' : ''}</p>`:'<p class="watch-note">Bàn chỉ quan sát · Tự cập nhật khi máy chấm chạy ván.</p>'}</div>`;
}

async function updateWatched() {
  await Promise.all([...watched.keys()].map(async id => {
    const data = await api(`/matches/${id}`), item = watched.get(id);
    if (!item) return;
    const signature = `${data.status}:${data.progress?.ply}:${data.replay?.finished}:${data.position}`;
    if (signature === item.signature) return;
    const first = !item.data;
    item.data = data; item.signature = signature;
    if (data.replay) {
      item.states = replayStates(data.replay.moves);
      // A finished game opened fresh plays from the start; one that just ended jumps to the result.
      item.position = first ? 0 : item.states.length - 1; item.playing = first;
    }
    renderWatch(id);
  }));
}

async function toggleWatch(id) {
  if (watched.has(id)) { watched.delete(id); document.getElementById(`watch-${id}`)?.remove(); }
  else {
    if (watched.size >= 4) throw new Error('Bạn đang xem bốn bàn. Bỏ một bàn để mở trận khác.');
    if (tab !== 'watch') setTab('watch');
    if (!watched.size) $('#watch-grid').replaceChildren();
    watched.set(id, {position:0, playing:false, speed:1, clock:0});
    const card = document.createElement('article'); card.className = 'watch-card'; card.id = `watch-${id}`; card.textContent = 'Đang tải bàn cờ…'; $('#watch-grid').append(card);
    try { await updateWatched(); } catch (error) { watched.delete(id); card.remove(); throw error; }
  }
  if (!watched.size) $('#watch-grid').innerHTML = '<div class="empty">Chọn một ván ở bên trái để theo dõi hoặc xem lại.</div>';
  $('#watch-count').textContent = `${watched.size} / 4 BÀN`;
}

function step(id, delta) {
  const item = watched.get(id);
  if (!item?.states) return;
  item.position = Math.max(0, Math.min(item.states.length-1, item.position+delta)); item.playing = false; renderWatch(id);
}

function play(id) {
  const item = watched.get(id);
  if (!item?.states) return;
  if (item.position === item.states.length-1) item.position = 0;
  item.playing = !item.playing; renderWatch(id);
}

async function copyLink(url, label) {
  try { await navigator.clipboard.writeText(url); notice(`Đã sao chép link ${label}. Gửi cho bạn bè để cùng xem!`); }
  catch { notice(url); }
}

// ---- refresh ------------------------------------------------------------

let loading = false;
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const [h, ts] = await Promise.all([api('/health'), api('/tournaments')]);
    health = h; tournaments = ts.items;
    renderHealth(); tournamentCards();
    if (me) submissions = team ? (await api('/submissions')).items : []; else submissions = [];
    if (team && (tab === 'overview' || tab === 'team')) {
      badges = (await cached(`badges:${team.id}`, 30000, () => api('/me/badges'))).items;
      announceBadges(badges);
    } else if (!team) badges = null;
    submissionRows(); renderTeam();
    if (tab === 'overview') {
      const [recent, r, f, c] = await Promise.all([team ? api('/matches?mine=1') : {items:[]}, api('/ranked'),
        cached('feed', 15000, () => api('/feed')), cached(`campaign:${team?.id}`, 30000, () => api('/campaign'))]);
      ranked = r; campaign = c; renderFeed(f); renderDashboard(recent.items);
      announceSeries(r.mine); announceRuns(c.mine);
    }
    if (tab === 'campaign') renderCampaign(await api('/campaign'));
    if (tab === 'ranked') renderRanked(await api('/ranked'));
    if (tab === 'competition') await renderCompetition();
    if (tab === 'watch') { await matchDirectory(); await updateWatched(); }
  } catch (error) {
    $('#service-status').textContent = error.status === 503 ? 'Máy chủ đang bận · tự thử lại' : 'Chưa kết nối được máy chủ';
    $('#service-status').classList.remove('online');
    $('#service-detail').textContent = 'Nội dung đang soạn vẫn được giữ trên máy.';
    throw error;
  } finally { loading = false; }
}
const busy = () => ['ranked','watch'].includes(tab) || (tab === 'campaign' && campaign?.mine?.runs.some(r => r.status === 'running'));
const refresher = poller(refresh, () => busy() ? 6000 : 15000);

// ---- events -------------------------------------------------------------

document.addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.close) $(`#${button.dataset.close}`).close();
  if (button.dataset.auth) openAuth(button.dataset.auth === 'register');
  if (button.dataset.tournament) { $('#team-dialog').close(); selectedTournament = button.dataset.tournament; setTab('competition'); }
  if (button.dataset.team) action(button, () => openTeam(button.dataset.team));
  if (button.dataset.copyTeam) copyLink(`${location.origin}/#team=${button.dataset.copyTeam}`, 'hồ sơ đội');
  if (button.dataset.boss) { selectedLevel = Number(button.dataset.boss); renderCampaign(campaign); $('#campaign-form').scrollIntoView({block:'nearest', behavior:'smooth'}); }
  if (button.dataset.bossBot) { setTab('campaign'); $('#campaign-submission').value = button.dataset.bossBot; }
  if (button.dataset.sample) action(button, async () => {
    const response = await fetch(`../sdk/examples/${button.dataset.sample}.py`);
    if (!response.ok) throw new Error('Không tải được bot mẫu.');
    $('#source').value = await response.text(); $('#bot-name').value = {first:'Nước đầu tiên',hunter:'Thợ săn',guardian:'Người gác'}[button.dataset.sample]; updateSize();
  });
  if (button.dataset.source) action(button, async () => { const result = await api(`/submissions/${button.dataset.source}/source`); $('#source').value = result.source; updateSize(); $('#source').focus(); });
  if (button.dataset.rankBot) { setTab('ranked'); $('#ranked-submission').value = button.dataset.rankBot; }
  if (button.dataset.watch) { if ($('#team-dialog').open) $('#team-dialog').close(); action(button, () => toggleWatch(button.dataset.watch)); }
  if (button.dataset.kick) action(button, async () => { if (!confirm('Mời thành viên này ra khỏi đội?')) return; await api('/team/kick', {user_id: button.dataset.kick}); await loadMe(); renderTeam(); });
  if (button.dataset.step && button.dataset.id) step(button.dataset.id, Number(button.dataset.step));
  if (button.dataset.play) play(button.dataset.play);
  if (button.dataset.speed) { const item = watched.get(button.dataset.speed); item.speed = {1:2, 2:4, 4:1}[item.speed]; renderWatch(button.dataset.speed); }
  if (button.dataset.expand) { const item = watched.get(button.dataset.expand); item.large = !item.large; renderWatch(button.dataset.expand); }
  if (button.dataset.share) copyLink(`${location.origin}/#watch=${button.dataset.share}`, 'ván đấu');
});
document.addEventListener('keydown', event => {
  if (tab !== 'watch' || watched.size !== 1 || event.target.closest('input,textarea,select,dialog') || event.ctrlKey || event.metaKey) return;
  const id = [...watched.keys()][0];
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); step(id, event.key === 'ArrowLeft' ? -1 : 1); }
  if (event.key === ' ' && !event.target.closest('button')) { event.preventDefault(); play(id); }
});
$('#competition-select').addEventListener('change', event => { selectedTournament = event.target.value; standingsLimit = 100; action(null, renderCompetition); });
$('#round-select').addEventListener('change', () => action(null, renderRound));
$('#standings-search').addEventListener('input', renderStandings);
$('#standings-more').addEventListener('click', () => { standingsLimit += 200; renderStandings(); });
$('#match-filter').addEventListener('change', () => action(null, matchDirectory));
document.addEventListener('input', event => {
  if (!event.target.dataset.replay) return;
  const id = event.target.dataset.replay, item = watched.get(id); item.position = Number(event.target.value); item.playing = false;
  const card = document.getElementById(`watch-${id}`), state = item.states[item.position];
  card.querySelector('.mini-board').outerHTML = board(state);
  card.querySelector('.material').outerHTML = material(state);
  card.querySelector('.result-banner')?.remove();
  card.querySelector('.watch-note').textContent = `Nước ${item.position}/${item.states.length-1}`;
});
document.addEventListener('change', event => { if (event.target.dataset.replay) renderWatch(event.target.dataset.replay); });
$('#source').addEventListener('input', updateSize);
$('#bot-name').addEventListener('input', updateSize);
$('#source').addEventListener('keydown', event => { if (event.key === 'Tab') { event.preventDefault(); const el = event.target; el.setRangeText('    ', el.selectionStart, el.selectionEnd, 'end'); updateSize(); } });
$('#source-file').addEventListener('change', () => action(null, async () => { const file = $('#source-file').files[0]; if (!file) return; if (file.size > 65536) throw new Error('File vượt quá 64 KiB.'); $('#source').value = await file.text(); $('#bot-name').value = file.name.replace(/\.py$/i, '').slice(0, 50); updateSize(); }));

$('#submit-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  await api('/submissions', {name: $('#bot-name').value, source: $('#source').value.replace(/\r\n/g, '\n')}); forget();
  notice('Đã lưu phiên bản. Kết quả kiểm tra sẽ cập nhật trong danh sách bên dưới.'); await refresh(); }); });
$('#practice-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/practice', {p1: $('#practice-a').value, p2: $('#practice-b').value});
  notice('Đã xếp hai ván đổi màu vào hàng chờ.'); await toggleWatch(result.ids[0]); }); });
$('#campaign-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  await api('/campaign/challenge', {level: Number($('#campaign-level').value), submission_id: $('#campaign-submission').value}); forget();
  notice('Đã vào ải! Hai ván với boss đang chờ máy chấm; kết quả hiện ở “Lượt gần đây”.'); await refresh(); }); });
$('#team-profile').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/friendly', {team_id: profile.id, submission_id: event.target.elements.submission.value}); forget();
  $('#team-dialog').close(); notice(`Đã gửi lời thách đấu tới ${profile.name}. Hai ván giao hữu đang chờ máy chấm.`); await toggleWatch(result.ids[0]); }); });
$('#ranked-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/ranked/queue', {submission_id: $('#ranked-submission').value});
  notice(result.status === 'matched' ? 'Đã ghép đối thủ. Hai ván đổi màu được xếp vào hàng chờ.' : 'Đã vào hàng chờ. Hệ thống sẽ tự ghép khi có đối thủ.'); await refresh(); }); });
$('#ranked-cancel').addEventListener('click', event => action(event.target, async () => { const result = await api('/ranked/cancel', {}); notice(result.cancelled ? 'Đã hủy tìm trận.' : 'Đối thủ vừa được ghép; cặp trận đã bắt đầu.'); await refresh(); }));
$('#join-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  await api(`/tournaments/${selectedTournament}/join`, {submission_id: $('#join-submission').value}); await loadMe();
  notice('Đã lưu phiên bản thi đấu của đội.'); await refresh(); }); });
$('#withdraw').addEventListener('click', event => action(event.target, async () => { if (!confirm('Rút đăng ký của đội khỏi giải này?')) return; await api(`/tournaments/${selectedTournament}/withdraw`, {}); await loadMe(); notice('Đã rút đăng ký.'); await refresh(); }));

$('#create-team-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => { await api('/team', {name: event.target.elements.name.value}); await loadMe(); forget(); notice('Đã tạo đội. Gửi mã đội cho các thành viên.'); await refresh(); }); });
$('#join-team-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => { await api('/team/join', {code: event.target.elements.code.value.trim().toUpperCase()}); await loadMe(); forget(); notice('Đã vào đội.'); await refresh(); }); });
$('#copy-code').addEventListener('click', async () => { try { await navigator.clipboard.writeText(team.code); notice('Đã sao chép mã đội.'); } catch { notice(`Mã đội: ${team.code}`); } });
$('#open-own-profile').addEventListener('click', event => action(event.target, () => openTeam(team.id)));
$('#new-code').addEventListener('click', event => action(event.target, async () => { if (!confirm('Mã cũ sẽ hết hiệu lực. Tiếp tục?')) return; await api('/team/code', {}); await loadMe(); renderTeam(); }));
$('#rename-team').addEventListener('click', event => action(event.target, async () => { const name = prompt('Tên đội mới (2–40 ký tự):', team.name); if (!name) return; await api('/team/rename', {name}); await loadMe(); renderTeam(); }));
$('#leave-team').addEventListener('click', event => action(event.target, async () => { if (!confirm('Rời đội? Bot và kết quả vẫn thuộc về đội.')) return; await api('/team/leave', {}); await loadMe(); forget(); notice('Đã rời đội.'); await refresh(); }));

$('#account-button').addEventListener('click', () => {
  if (!me) return openAuth(false);
  $('#account-name').textContent = me.name;
  $('#account-detail').textContent = `@${me.username}${team ? ` · Đội ${team.name}` : ' · Chưa có đội'}`;
  $('#account-dialog').showModal();
});
$('#logout').addEventListener('click', event => action(event.target, async () => { await api('/logout', {}); me = team = session.csrf = null; forget(); $('#account-dialog').close(); renderAccount(); setTab('overview'); }));
$('#change-password').addEventListener('click', () => { $('#account-dialog').close(); $('#password-dialog').showModal(); });
$('#open-password').addEventListener('click', () => $('#password-dialog').showModal());
$('#password-form').addEventListener('submit', async event => { event.preventDefault(); $('#password-error').textContent = '';
  try { await api('/me/password', Object.fromEntries(new FormData(event.target))); event.target.reset(); $('#password-dialog').close(); await loadMe(); notice('Đã đổi mật khẩu.'); }
  catch (error) { $('#password-error').textContent = error.message; } });
$('#toggle-auth').addEventListener('click', () => {
  register = !register; const form = $('#auth-form');
  $('#register-fields').hidden = !register; form.elements.name.required = register; form.elements.password.minLength = register ? 12 : 1;
  form.elements.password.autocomplete = register ? 'new-password' : 'current-password';
  $('#auth-title').textContent = register ? 'Tạo tài khoản thi đấu.' : 'Trở lại bàn cờ.';
  $('#auth-submit').textContent = register ? 'Tạo tài khoản' : 'Đăng nhập';
  $('#toggle-auth').textContent = register ? 'Đã có tài khoản? Đăng nhập' : 'Chưa có tài khoản? Đăng ký'; $('#auth-error').textContent = '';
});
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); event.submitter.disabled = true; $('#auth-error').textContent = '';
  const data = Object.fromEntries(new FormData(event.target)); data.username = data.username.trim().toLowerCase();
  try {
    await api(register ? '/register' : '/login', data);
    event.target.elements.password.value = ''; $('#auth-dialog').close(); await loadMe(); forget();
    if (register) { notice('Chào mừng! Bước tiếp theo: lập đội hoặc vào đội bằng mã.'); setTab('team'); } else refresher.now();
  } catch (error) { $('#auth-error').textContent = error.message; }
  finally { event.submitter.disabled = false; }
});

// Replay clock: ticks often so 2× and 4× stay smooth; one move per 650 ms at 1×.
setInterval(() => {
  if (document.hidden || tab !== 'watch') return;
  for (const [id, item] of watched) {
    if (!item.playing || !item.states) continue;
    item.clock += 130 * item.speed;
    if (item.clock < 650) continue;
    item.clock = 0;
    item.position = Math.min(item.position+1, item.states.length-1);
    if (item.position === item.states.length-1) item.playing = false;
    renderWatch(id);
  }
}, 130);

const draft = (() => { try { return JSON.parse(storage(DRAFT) || 'null'); } catch { return null; } })();
if (draft?.source) { $('#source').value = draft.source; $('#bot-name').value = draft.name || ''; }
else {
  $('#source').value = "def choose_move(observation):\n    # Thay chiến thuật của bạn tại đây.\n    move = observation['legalMoves'][0]\n    return {'from': move['from'], 'to': move['to']}\n";
  try { const sample = await fetch('../sdk/examples/first.py'); if (sample.ok) { $('#source').value = await sample.text(); $('#bot-name').value = 'Nước đầu tiên'; } } catch {}
}
$('#source-size').textContent = `${new TextEncoder().encode($('#source').value).length.toLocaleString('vi-VN')} B`;
await loadMe();
// Shared links: /#watch=<match> opens a replay, /#team=<team> opens a profile.
const shared = location.hash.slice(1);
if (shared.startsWith('watch=')) { setTab('watch'); action(null, () => toggleWatch(shared.slice(6))); }
else if (shared.startsWith('team=')) { setTab('overview'); action(null, () => openTeam(shared.slice(5))); }
else setTab(shared || 'overview');
