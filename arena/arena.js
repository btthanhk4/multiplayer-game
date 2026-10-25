import {$, $$, escape, labels, formats, kinds, formatTime, badge, session, api, notice, action, options, countdown,
  poller, outcome, teamLink, starText, ago, sparkline, celebrate} from './common.js';
import {createWatch} from './watch.js';

document.getElementById('boot-note')?.remove();   // reaching here means every module loaded

let me = null, team = null, health = null, tab = 'overview', register = false;
let tournaments = [], submissions = [], selectedTournament = '', competition = null, standingsLimit = 100, ranked = null;
let campaign = null, badges = null, selectedLevel = null, profile = null;
const DRAFT = 'ottv2-draft';
const TABS = ['overview','team','lab','campaign','ranked','competition','watch'];
const CLEAR = 1.5;
const watch = createWatch({team: () => team, tab: () => tab, setTab: value => setTab(value)});
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
  history.replaceState(null, '', `${location.pathname}#${tab}`);
  refresher.now();
  watch.scheduleLive(100);
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

function setMode(asRegister) {
  register = asRegister;
  const form = $('#auth-form');
  $('#register-fields').hidden = $('#register-more').hidden = !register;
  $('#username-hint').hidden = $('#password-hint').hidden = !register;
  form.elements.invite.required = register && Boolean(health?.registration_invite);
  form.elements.name.required = register; form.elements.password.minLength = register ? 12 : 1;
  form.elements.password.autocomplete = register ? 'new-password' : 'current-password';
  $('#tab-login').setAttribute('aria-selected', String(!register)); $('#tab-register').setAttribute('aria-selected', String(register));
  $('#auth-title').textContent = register ? 'Tạo tài khoản thi đấu.' : 'Trở lại bàn cờ.';
  $('#auth-sub').textContent = register ? 'Mất chưa tới một phút. Sau đó lập đội hoặc vào đội bằng mã bạn bè gửi.' : 'Đăng nhập để vào đấu trường. Tài khoản là bắt buộc để xem và thi đấu.';
  $('#auth-submit').textContent = register ? 'Tạo tài khoản' : 'Đăng nhập';
  $('#toggle-auth').textContent = register ? 'Đã có tài khoản? Đăng nhập' : 'Chưa có tài khoản? Tạo tài khoản miễn phí';
  $('#auth-error').textContent = '';
}

function openAuth(asRegister) {
  if (me) return;
  setMode(asRegister);
  $('#gate').hidden = false;
}

async function loadMe() {
  try {
    const result = await api('/me');
    me = result.user; team = result.team; session.csrf = result.csrf;
  } catch { me = team = null; }
  renderAccount();
}

function renderAccount() {
  document.body.classList.toggle('guest', !me);
  $('#gate').hidden = Boolean(me);
  $('#account-button').textContent = me ? `${me.name}${team ? ` · ${team.name}` : ''}` : 'Đăng nhập';
  $('#admin-link').hidden = me?.role !== 'admin';
  $('#password-banner').hidden = !me?.must_change;
}

const skew = () => health ? health.server_time - Date.now()/1000 : 0;

// Rough wait for a job that is `position` places back: places divided across the running slots, times the
// recent average job time. Shown as an estimate only; a deep queue or slow bots change it.
function etaText(position, kind) {
  const slots = health?.runner_slots, average = health?.avg_seconds?.[kind] ?? (kind === 'validate' ? 3 : 12);
  if (!position || !slots) return '';
  const seconds = Math.max(5, Math.ceil(position / slots) * average);
  return ` (ước tính ${seconds < 90 ? `${Math.round(seconds)} giây` : `${Math.round(seconds / 60)} phút`})`;
}

function renderHealth() {
  $('#service-status').textContent = health.open.read_only ? 'Đang bảo trì · chỉ xem' : health.runner_online ? `Máy chấm đang chạy · ${health.runner_slots} luồng` : 'Bài nộp được lưu · Chờ máy chấm';
  $('#service-status').classList.toggle('online', health.runner_online && !health.open.read_only);
  $('#service-detail').textContent = health.runner_online && health.runner_modes.some(m => m !== 'docker') ? 'Đang thử nội bộ · Chưa chấm giải chính thức' : `${health.pending} lượt chấm đang chờ hoặc đang chạy`;
  $('#announcement').hidden = !health.announcement;
  $('#announcement').textContent = health.announcement;
  $('#invite-field').hidden = !health.registration_invite;
  $('#team-caption').textContent = `Tối đa ${health.team_size} thành viên. Bot, Elo, huy hiệu và đăng ký giải thuộc về đội.`;
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
  const entered = (team?.entries || []).length > 0;
  const cleared = campaign?.mine?.cleared || 0, levels = campaign?.bosses?.length || 5;
  const steps = [
    {label: 'Lập đội', done: Boolean(team), title: 'Lập đội hoặc vào đội của bạn', text: 'Tạo đội mới rồi gửi mã đội cho bạn cùng nhóm, hoặc nhập mã bạn bè đã gửi.', cta: 'Lập đội', tab: 'team'},
    {label: 'Nộp bot', done: valid.length > 0, title: 'Nộp bot đầu tiên', text: 'Chọn một bot mẫu, bấm nộp và chờ máy kiểm tra (vài chục giây).', cta: 'Mở xưởng bot', tab: 'lab'},
    {label: 'Hạ Boss 1', done: cleared > 0, title: 'Hạ boss đầu tiên', text: 'Boss 1 rất dễ: chỉ cần bot chạy đúng là thắng.', cta: 'Vào chiến dịch', tab: 'campaign'},
    ...(openTournaments().length || entered ? [{label: 'Đăng ký giải', done: entered, title: 'Đăng ký giải đang mở', text: 'Chọn bot trước hạn để giữ suất cho đội.', cta: 'Xem giải', tab: 'competition'}] : []),
    {label: 'Xem replay', done: recent.some(m => m.status === 'done'), title: 'Xem một ván đấu', text: 'Mở khán đài để xem lại cách bot của bạn chơi.', cta: 'Mở khán đài', tab: 'watch'},
  ];
  const current = steps.find(step => !step.done);
  $('#next-step').innerHTML = `<div class="next-main"><p class="eyebrow">${current ? `VIỆC TIẾP THEO · ${steps.filter(s => s.done).length}/${steps.length}` : 'XONG LỘ TRÌNH'}</p><h2>${escape(current?.title || 'Bạn đã sẵn sàng thi đấu')}</h2><p>${escape(current?.text || 'Tiếp tục leo bảng Elo, hoặc luyện thêm bằng các boss cao hơn.')}</p><button class="button primary" data-tab="${current?.tab || 'ranked'}">${escape(current?.cta || 'Đấu xếp hạng')} ↗</button></div><ol class="progress-dots" aria-label="Tiến độ">${steps.map(step => `<li class="${step.done ? 'done' : step === current ? 'now' : ''}"><span aria-hidden="true">${step.done ? '✓' : ''}</span>${escape(step.label)}</li>`).join('')}</ol>`;
  const mine = ranked?.mine;
  const tiles = [
    ['team', 'ĐỘI', team ? team.name : 'Chưa có đội', team ? `${team.members.length} thành viên` : 'Lập đội để bắt đầu'],
    ['lab', 'BOT', latest ? latest.name : 'Chưa có bot', latest ? (labels[latest.status] || latest.status) : 'Nộp bot đầu tiên'],
    ['campaign', 'CHIẾN DỊCH', `Ải ${cleared}/${levels}`, `${campaign?.mine?.stars || 0} sao`],
    ['ranked', 'ELO', mine ? `${mine.rating} · ${mine.tier}` : '1000 · Bạc', mine?.position ? `Hạng #${mine.position}` : 'Chưa xếp hạng'],
  ];
  $('#dashboard-cards').innerHTML = tiles.map(([tabName, label, value, hint]) => `<button class="tile" data-tab="${tabName}"><span class="eyebrow">${label}</span><strong>${escape(value)}</strong><small>${escape(hint)}</small></button>`).join('');
  const open = openTournaments()[0];
  const registered = open && (team?.entries || []).some(e => e.tournament_id === open.id);
  $('#open-tournament').innerHTML = open ? `<div class="banner"><div><p class="eyebrow">GIẢI ĐANG MỞ · ${escape(countdown(open.deadline).toUpperCase())}</p><h3>${escape(open.name)}</h3><p class="caption">${registered ? 'Đội đã đăng ký. Có thể đổi bot trước hạn.' : `Hạn chọn bot: ${formatTime(open.deadline)}`}</p></div><button class="button ${registered ? '' : 'primary'}" data-tournament="${open.id}">${registered ? 'Xem giải' : 'Đăng ký ngay'} ↗</button></div>` : '';
}

const TAGS = {upset:['hot','LẬT KÈO'], sweep:['','THẮNG TRẮNG'], final:['hot','TRÙM CUỐI'], champion:['gold','VÔ ĐỊCH']};

function renderFeed(data) {
  const s = data.stats;
  $('#arena-stats-line').textContent = `${Number(s.teams_ready).toLocaleString('vi-VN')} đội đã có bot · ${Number(s.games_today).toLocaleString('vi-VN')} ván trong 24 giờ · ${s.live} đang đấu`;
  $('#feed').innerHTML = data.events.length ? data.events.slice(0, 6).map(e => {
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

function tournamentCards() {
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
    : submissions.length ? submissions.map(s => `<article class="submission"><div><strong>${escape(s.name)}</strong><small>${formatTime(s.created)} · ${escape(s.author || (s.origin === 'form' ? 'Google Form' : ''))} · ${escape(s.hash.slice(0,10))}</small></div>${badge(s.status)}<div class="submission-log">${s.position ? `Vị trí #${s.position} trong hàng chờ kiểm tra${etaText(s.position, 'validate')}.` : escape(s.log || 'Bài đã được lưu. Chờ máy chấm tiếp nhận.')}</div><div class="submission-actions"><button class="text-link" data-source="${s.id}">Mở mã ↗</button>${s.status==='valid'?`<button class="text-link" data-boss-bot="${s.id}">Đánh boss ↗</button><button class="text-link" data-rank-bot="${s.id}">Tìm đối thủ ↗</button>`:''}</div></article>`).join('')
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
      : `Đang đấu Boss ${running.level} · ${running.boss}. ${position ? `Ván đang ở vị trí #${position} trong hàng chờ${etaText(position, 'game')}.` : 'Hai ván đang chạy trên server.'}`)
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
      : `Đã ghép với ${active.a===team.id?active.b_name:active.a_name}. ${position ? `Ván đang ở vị trí #${position} trong hàng chờ${etaText(position, 'game')}.` : 'Hai ván đang chạy trên server.'}`
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

// ---- watch (see watch.js) ------------------------------------------------

let mineLive = [];
const autoOpened = new Set();

async function loadMineLive() {
  if (!team) { mineLive = []; renderLiveBanner(); return; }
  try { mineLive = (await api('/me/live')).items; } catch { return; }
  renderLiveBanner();
  if (tab === 'watch' && !watch.size()) {
    const running = mineLive.find(m => m.status === 'running' && !autoOpened.has(m.id));
    if (running) { autoOpened.add(running.id); await watch.toggle(running.id); renderLiveBanner(); }
  }
}

function renderLiveBanner() {
  const pending = mineLive.filter(m => !watch.has(m.id)), el = $('#live-banner');
  if (!pending.length) { el.hidden = true; el.replaceChildren(); return; }
  const m = pending[0], running = m.status === 'running';
  el.hidden = false;
  el.innerHTML = `<span class="live-pill ${running ? '' : 'wait'}">${running ? '● ĐANG ĐẤU' : 'ĐANG CHỜ'}</span><span><strong>${escape(m.team1)}</strong> × <strong>${escape(m.team2)}</strong> · ${escape(kinds[m.kind] || '')}${!running && m.position ? ` · vị trí #${m.position} trong hàng chờ${etaText(m.position, 'game')}` : ''}${pending.length > 1 ? ` · và ${pending.length - 1} ván khác` : ''}</span><button class="button ${running ? 'primary' : ''}" data-watch="${m.id}">${running ? 'Xem trực tiếp' : 'Theo dõi'} ↗</button>`;
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
    if (!me) { health = await api('/health'); renderHealth(); return; }
    const [h, ts] = await Promise.all([api('/health'), api('/tournaments')]);
    health = h; tournaments = ts.items;
    renderHealth(); tournamentCards();
    if (me) submissions = team ? (await api('/submissions')).items : []; else submissions = [];
    if (team && (tab === 'overview' || tab === 'team')) {
      badges = (await cached(`badges:${team.id}`, 30000, () => api('/me/badges'))).items;
      announceBadges(badges);
    } else if (!team) badges = null;
    submissionRows(); renderTeam();
    await loadMineLive();
    if (tab === 'overview') {
      const [recent, r, f, c] = await Promise.all([team ? api('/matches?mine=1') : {items:[]}, api('/ranked'),
        cached('feed', 15000, () => api('/feed')), cached(`campaign:${team?.id}`, 30000, () => api('/campaign'))]);
      ranked = r; campaign = c; renderFeed(f); renderDashboard(recent.items);
      announceSeries(r.mine); announceRuns(c.mine);
    }
    if (tab === 'campaign') renderCampaign(await api('/campaign'));
    if (tab === 'ranked') renderRanked(await api('/ranked'));
    if (tab === 'competition') await renderCompetition();
    if (tab === 'watch') await watch.directory();
  } catch (error) {
    $('#service-status').textContent = error.status === 503 ? 'Máy chủ đang bận · tự thử lại' : 'Chưa kết nối được máy chủ';
    $('#service-status').classList.remove('online');
    $('#service-detail').textContent = 'Nội dung đang soạn vẫn được giữ trên máy.';
    throw error;
  } finally { loading = false; }
}
const busy = () => ['ranked','watch'].includes(tab) || mineLive.length > 0 || Boolean(ranked?.mine?.queued) || (tab === 'campaign' && campaign?.mine?.runs.some(r => r.status === 'running'));
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
    $('#source').value = await response.text(); $('#bot-name').value = {first:'Nước đầu tiên',hunter:'Thợ săn',guardian:'Người gác',template:'Khung chiến thuật'}[button.dataset.sample]; updateSize();
  });
  if (button.dataset.source) action(button, async () => { const result = await api(`/submissions/${button.dataset.source}/source`); $('#source').value = result.source; updateSize(); $('#source').focus(); });
  if (button.dataset.rankBot) { setTab('ranked'); $('#ranked-submission').value = button.dataset.rankBot; }
  watch.onClick(button);
  if (button.dataset.watch) { if ($('#team-dialog').open) $('#team-dialog').close(); action(button, async () => { await watch.toggle(button.dataset.watch); renderLiveBanner(); if (tab === 'watch') await watch.directory(); }); }
  if (button.dataset.kick) action(button, async () => { if (!confirm('Mời thành viên này ra khỏi đội?')) return; await api('/team/kick', {user_id: button.dataset.kick}); await loadMe(); renderTeam(); });
});
document.addEventListener('keydown', watch.onKey);
$('#competition-select').addEventListener('change', event => { selectedTournament = event.target.value; standingsLimit = 100; action(null, renderCompetition); });
$('#round-select').addEventListener('change', () => action(null, renderRound));
$('#standings-search').addEventListener('input', renderStandings);
$('#show-detail').addEventListener('change', event => $('#standings-table').classList.toggle('compact', !event.target.checked));
$('#standings-more').addEventListener('click', () => { standingsLimit += 200; renderStandings(); });
for (const id of ['#match-filter', '#match-kind']) $(id).addEventListener('change', () => action(null, watch.directory));
let directoryTimer;
$('#match-search').addEventListener('input', () => { clearTimeout(directoryTimer); directoryTimer = setTimeout(() => action(null, watch.directory), 300); });
$('#watch-random').addEventListener('click', event => action(event.target, async () => { await watch.randomLive(); await watch.directory(); }));
document.addEventListener('input', watch.onInput);
$('#source').addEventListener('input', updateSize);
$('#bot-name').addEventListener('input', updateSize);
$('#source').addEventListener('keydown', event => { if (event.key === 'Tab') { event.preventDefault(); const el = event.target; el.setRangeText('    ', el.selectionStart, el.selectionEnd, 'end'); updateSize(); } });
$('#source-file').addEventListener('change', () => action(null, async () => { const file = $('#source-file').files[0]; if (!file) return; if (file.size > 65536) throw new Error('File vượt quá 64 KiB.'); $('#source').value = await file.text(); $('#bot-name').value = file.name.replace(/\.py$/i, '').slice(0, 50); updateSize(); }));

$('#submit-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  await api('/submissions', {name: $('#bot-name').value, source: $('#source').value.replace(/\r\n/g, '\n')}); forget();
  notice('Đã lưu phiên bản. Kết quả kiểm tra sẽ cập nhật trong danh sách bên dưới.'); await refresh(); }); });
$('#practice-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/practice', {p1: $('#practice-a').value, p2: $('#practice-b').value});
  notice('Đã xếp hai ván đổi màu vào hàng chờ.'); await watch.toggle(result.ids[0]); }); });
$('#campaign-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  await api('/campaign/challenge', {level: Number($('#campaign-level').value), submission_id: $('#campaign-submission').value}); forget();
  notice('Đã vào ải! Hai ván với boss đang chờ máy chấm. Bấm “Xem trực tiếp” ở thanh phía trên để theo dõi.'); await refresh(); }); });
$('#team-profile').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/friendly', {team_id: profile.id, submission_id: event.target.elements.submission.value}); forget();
  $('#team-dialog').close(); notice(`Đã gửi lời thách đấu tới ${profile.name}. Hai ván giao hữu đang chờ máy chấm.`); await watch.toggle(result.ids[0]); }); });
$('#ranked-form').addEventListener('submit', event => { event.preventDefault(); if (!requireTeam()) return; action(event.submitter, async () => {
  const result = await api('/ranked/queue', {submission_id: $('#ranked-submission').value});
  notice(result.status === 'matched' ? 'Đã ghép đối thủ. Bấm “Xem trực tiếp” ở thanh phía trên để theo dõi hai ván.' : 'Đã vào hàng chờ. Khi ghép được đối thủ, thanh phía trên sẽ báo để bạn xem trực tiếp.'); await refresh(); }); });
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
$('#logout').addEventListener('click', event => action(event.target, async () => { await api('/logout', {}); me = team = badges = campaign = session.csrf = null; mineLive = []; forget(); $('#account-dialog').close(); watch.clear(); renderLiveBanner(); renderAccount(); setMode(false); refresher.now(); }));
$('#change-password').addEventListener('click', () => { $('#account-dialog').close(); $('#password-dialog').showModal(); });
$('#open-password').addEventListener('click', () => $('#password-dialog').showModal());
$('#password-form').addEventListener('submit', async event => { event.preventDefault(); $('#password-error').textContent = '';
  try { await api('/me/password', Object.fromEntries(new FormData(event.target))); event.target.reset(); $('#password-dialog').close(); await loadMe(); notice('Đã đổi mật khẩu.'); }
  catch (error) { $('#password-error').textContent = error.message; } });
$('#toggle-auth').addEventListener('click', () => setMode(!register));
const slug = text => text.normalize('NFD').replace(/đ/g, 'd').replace(/Đ/g, 'D').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 24);
let usernameTouched = false;
$('#auth-form').elements.username.addEventListener('input', event => { usernameTouched = true; event.target.value = event.target.value.replace(/\s+/g, '').toLowerCase(); });
$('#auth-form').elements.name.addEventListener('input', event => {
  const field = $('#auth-form').elements.username;
  if (register && (!usernameTouched || !field.value)) { field.value = slug(event.target.value); usernameTouched = false; }
});
$('#show-password').addEventListener('click', event => {
  const field = $('#auth-form').elements.password, show = field.type === 'password';
  field.type = show ? 'text' : 'password'; event.target.textContent = show ? 'Ẩn' : 'Hiện'; event.target.setAttribute('aria-pressed', String(show));
});
$('#tab-login').addEventListener('click', () => setMode(false));
$('#tab-register').addEventListener('click', () => setMode(true));
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#auth-error').textContent = '';
  if (!event.target.reportValidity()) return;
  event.submitter.disabled = true;
  const data = Object.fromEntries(new FormData(event.target)); data.username = data.username.trim().toLowerCase();
  if (!register) { delete data.name; delete data.student_id; delete data.invite; }
  if (register && /^\s|\s$/.test(data.password)) { $('#auth-error').textContent = 'Mật khẩu không được bắt đầu hoặc kết thúc bằng khoảng trắng.'; event.submitter.disabled = false; return; }
  if (!/^[a-z0-9_]{3,32}$/.test(data.username)) { $('#auth-error').textContent = 'Tên đăng nhập chỉ gồm chữ thường không dấu, số và dấu gạch dưới (3–32 ký tự).'; event.submitter.disabled = false; return; }
  try {
    await api(register ? '/register' : '/login', data);
    event.target.elements.password.value = ''; await loadMe(); forget();
    if (register) { notice('Chào mừng! Bước tiếp theo: lập đội hoặc vào đội bằng mã.'); setTab('team'); } else route();
  } catch (error) { $('#auth-error').textContent = error.message; }
  finally { event.submitter.disabled = false; }
});

const draft = (() => { try { return JSON.parse(storage(DRAFT) || 'null'); } catch { return null; } })();
if (draft?.source) { $('#source').value = draft.source; $('#bot-name').value = draft.name || ''; }
else {
  $('#source').value = "def choose_move(observation):\n    # Thay chiến thuật của bạn tại đây.\n    move = observation['legalMoves'][0]\n    return {'from': move['from'], 'to': move['to']}\n";
  try { const sample = await fetch('../sdk/examples/first.py'); if (sample.ok) { $('#source').value = await sample.text(); $('#bot-name').value = 'Nước đầu tiên'; } } catch {}
}
$('#source-size').textContent = `${new TextEncoder().encode($('#source').value).length.toLocaleString('vi-VN')} B`;
await loadMe();
// Shared links: /#watch=<match> opens a replay, /#team=<team> opens a profile.
function route() {
  const shared = location.hash.slice(1);
  if (shared.startsWith('watch=')) { const [id, ply] = shared.slice(6).split('&ply='); setTab('watch'); action(null, () => watch.toggle(id, ply === undefined ? {} : {ply: Number(ply)})); }
  else if (shared.startsWith('team=')) { setTab('overview'); action(null, () => openTeam(shared.slice(5))); }
  else setTab(shared || 'overview');
}
if (me) route(); else { setMode(false); refresher.now(); }
