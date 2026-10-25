import {$, $$, escape, labels, formats, kinds, formatTime, badge, session, api, notice, action, options, countdown,
  duration, poller, outcome, mapLabel, roundRulesHtml} from './common.js';

document.getElementById('boot-note')?.remove();   // reaching here means every module loaded
let editingFor = '';
let me = null, tab = 'dashboard', tournaments = [], adminTournament = '', detail = null;

function setTab(value) {
  if (!$$('.view').some(el => el.id === value)) value = 'dashboard';
  tab = value;
  $$('.view').forEach(el => el.hidden = el.id !== tab);
  $$('[role=tab]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.tab === tab)));
  history.replaceState(null, '', `${location.pathname}#${tab}`);
  refresher.now();
}

async function download(path, fallback) {
  const response = await api(path, undefined, {raw: true});
  const name = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || fallback;
  const link = Object.assign(document.createElement('a'), {href: URL.createObjectURL(await response.blob()), download: name});
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}

const count = (object, key) => object?.[key] || 0;
const sum = object => Object.values(object || {}).reduce((a, b) => a + b, 0);

// ---- dashboard ----------------------------------------------------------

async function renderDashboard() {
  const [o, settings] = await Promise.all([api('/admin/overview'), api('/admin/settings')]);
  const queued = sum(Object.fromEntries(Object.entries(o.jobs).map(([k, v]) => [k, count(v, 'queued')])));
  const failed = sum(Object.fromEntries(Object.entries(o.jobs).map(([k, v]) => [k, count(v, 'failed')])));
  const running = sum(Object.fromEntries(Object.entries(o.jobs).map(([k, v]) => [k, count(v, 'running')])));
  $('#service-status').textContent = o.active_slots ? `${o.active_slots} luồng chấm đang hoạt động` : 'Không có máy chấm nào trực tuyến';
  $('#service-status').classList.toggle('online', o.active_slots > 0);
  $('#service-detail').textContent = `${queued} chờ · ${running} đang chạy · ${failed} lỗi`;
  const tiles = [
    ['Tài khoản', o.users, `${o.sessions} phiên đăng nhập còn hạn`],
    ['Đội', o.teams, `${o.teams_with_valid_bot} đội có bot hợp lệ`],
    ['SV chưa có đội', o.players_without_team, 'Nhắc lập đội trước hạn', o.players_without_team > 0 ? 'warn' : ''],
    ['Bài nộp', sum(o.submissions), `${count(o.submissions,'valid')} hợp lệ · ${count(o.submissions,'rejected')} cần sửa`],
    ['Hàng đợi', queued, `${running} đang chạy`],
    ['Job lỗi', failed, failed ? 'Mở “Hàng đợi” để thử lại' : 'Không có', failed ? 'bad' : ''],
    ['Luồng chấm', o.active_slots, Object.entries(o.workers).map(([m, n]) => `${n} ${m}`).join(' · ') || 'Chưa có runner', o.active_slots ? '' : 'bad'],
    ['Ước tính xong', !o.queued ? 'Trống' : o.eta_seconds == null ? 'Chưa có máy chấm' : duration(o.eta_seconds), `${o.finished_last_hour} job xong trong 1 giờ`],
    ['Hàng chờ xếp hạng', o.ranked_waiting, `đội đang tìm đối thủ · ${o.campaign_running} lượt đánh boss đang chạy`],
    ['Cơ sở dữ liệu', `${(o.database_bytes/1048576).toFixed(1)} MB`, 'Sao lưu trước và sau mỗi vòng'],
  ];
  $('#stats').innerHTML = tiles.map(([label, value, hint, tone]) => `<article class="stat ${tone || ''}"><span>${escape(label)}</span><strong>${escape(value)}</strong><small>${escape(hint)}</small></article>`).join('');
  const rows = Object.entries(o.jobs).map(([kind, states]) => `<tr><td>${kind === 'validate' ? 'Kiểm tra bot' : 'Ván đấu'}</td><td>${count(states,'queued')}</td><td>${count(states,'running')}</td><td>${count(states,'done')}</td><td>${count(states,'failed')}</td><td>${duration(o.average_seconds[kind])}</td></tr>`).join('');
  const games = Object.entries(o.games).map(([kind, states]) => `${escape(kinds[kind] || kind)}: ${count(states,'queued')} chờ / ${count(states,'running')} chạy / ${count(states,'done')} xong`).join('<br>');
  const advice = o.eta_seconds > 7200 ? '<p class="notice error">Hàng đợi cần hơn 2 giờ. Tăng --slots (≈ số lõi CPU trống), chạy thêm runner trên máy khác dùng chung dữ liệu, hoặc tạm tắt đấu xếp hạng/đấu tập.</p>' : '';
  $('#capacity').innerHTML = `${advice}<div class="table-scroll"><table><thead><tr><th>LOẠI</th><th>CHỜ</th><th>CHẠY</th><th>XONG</th><th>LỖI</th><th>TB/JOB</th></tr></thead><tbody>${rows || '<tr><td colspan="6">Chưa có job.</td></tr>'}</tbody></table></div><p class="caption">${games || ''}</p>`;
  $('#boss-status').innerHTML = o.bosses.length ? `<div class="table-scroll"><table><thead><tr><th>ẢI</th><th>BOSS</th><th>TRẠNG THÁI</th></tr></thead><tbody>${o.bosses.map(b => `<tr><td>${b.level}</td><td>${escape(b.name)}</td><td>${badge(b.status)}</td></tr>`).join('')}</tbody></table></div><p class="caption">Boss được kiểm tra như bài nộp thường khi server khởi động hoặc khi file boss thay đổi. Thí sinh chỉ vào được ải khi boss đã “Hợp lệ”.</p>`
    : '<p class="caption">Chưa đăng ký boss. Khởi động lại server API để tạo.</p>';
  const form = $('#settings-form');
  if (!form.contains(document.activeElement)) {
    for (const key of ['registration_open','submissions_open','ranked_open','practice_open','campaign_open','live_open','read_only']) form.elements[key].checked = settings[key] === '1';
    form.elements.announcement.value = settings.announcement;
  }
}

// ---- tournaments --------------------------------------------------------

async function loadTournaments() {
  tournaments = (await api('/tournaments')).items;
  options('#admin-tournament', tournaments, 'Chưa có giải đấu', t => `${t.name} · ${labels[t.status]}`);
  if (!tournaments.some(t => t.id === adminTournament)) adminTournament = tournaments[0]?.id || '';
  $('#admin-tournament').value = adminTournament;
  const completed = tournaments.filter(t => t.status === 'completed');
  const source = $('#seed-source'), before = source.value;
  source.innerHTML = '<option value="">Đăng ký mở (thí sinh tự chọn bot)</option>' + completed.map(t => `<option value="${escape(t.id)}">Top N của: ${escape(t.name)}</option>`).join('');
  if (completed.some(t => t.id === before)) source.value = before;
}

let howtoChecked = false;
async function renderEvents() {
  await loadTournaments();
  if (!adminTournament) { $('#next-action').innerHTML = '<strong>Bước 1/5 · Việc cần làm tiếp theo</strong><p>Chưa có giải nào. Điền form “Mở giải” (hoặc bấm một mẫu ở phần hướng dẫn phía trên) rồi bấm “Tạo giải”.</p>'; for (const li of $$('#stepper li')) li.className = li.dataset.step === '1' ? 'current' : ''; $('#tournament-state').textContent = 'Tạo giải đầu tiên ở bên trái.'; $('#entry-body').innerHTML = ''; return; }
  if (!howtoChecked) { howtoChecked = true; $('#howto').open = false; }   // the guide opens itself only until the first tournament exists
  detail = await api(`/tournaments/${adminTournament}?limit=300&q=${encodeURIComponent($('#entry-search').value.trim())}`);
  const t = detail.tournament, active = detail.active;
  const pending = detail.pending;
  $('#tournament-state').innerHTML = `${badge(t.status)} ${escape(formats[t.format])} · Vòng ${t.round}/${t.rounds} · Bản đồ ${mapLabel(t.map)}${t.final_map ? ` (chung kết: ${mapLabel(t.final_map)})` : ''} · ${detail.count} đăng ký${t.status !== 'open' ? ` · ${active} đang thi` : ''}${pending && t.status === 'open' ? ` · <strong>${pending} bot đang chờ kiểm tra</strong>` : ''}<br>${t.status === 'open' ? `Hạn: ${formatTime(t.deadline)} (${countdown(t.deadline)})` : `${detail.unfinished} ván chưa xong`}`;
  renderGuidance(t, detail);
  const rulesHtml = roundRulesHtml(t), rulesBox = $('#admin-rules');
  if (rulesBox.dataset.html !== rulesHtml) { rulesBox.dataset.html = rulesHtml; rulesBox.innerHTML = `<details class="more"><summary>Luật từng vòng (thí sinh cũng thấy nội dung này)</summary>${rulesHtml}</details>`; }
  $('#freeze-button').disabled = $('#force-freeze').disabled = t.status !== 'open' || t.deadline > Date.now()/1000;
  $('#next-round-button').disabled = !['frozen','running'].includes(t.status) || detail.unfinished > 0;
  $('#auto-toggle').checked = Boolean(t.auto_advance);
  const form = $('#tournament-edit'), open = t.status === 'open', ended = ['completed', 'cancelled'].includes(t.status);
  if (editingFor !== t.id) {   // only when the selection changes, so typing is not overwritten by the 5 s refresh
    editingFor = t.id;
    form.elements.name.value = t.name; form.elements.description.value = t.description || ''; form.elements.map.value = t.map || 'standard'; form.elements.final_map.value = t.final_map || '';
    const local = new Date(t.deadline * 1000); local.setMinutes(local.getMinutes() - local.getTimezoneOffset());
    form.elements.deadline.value = local.toISOString().slice(0, 16); form.elements.rounds.value = t.rounds;
  }
  $('#edit-deadline-field').hidden = !open || Boolean(t.seed_from);
  $('#edit-rounds-field').hidden = !open || t.format !== 'swiss';
  $('#edit-map').disabled = $('#edit-final-map').disabled = ended;
  $('#close-now').disabled = !open || Boolean(t.seed_from);
  $('#cancel-tournament').disabled = ended;
  $('#add-entry').hidden = !open || Boolean(t.seed_from);
  const roundSelect = $('#admin-round'), before = roundSelect.value;
  roundSelect.innerHTML = t.round ? Array.from({length: t.round}, (_, i) => `<option value="${i + 1}">Vòng ${i + 1}</option>`).join('') : '<option value="">Chưa có vòng</option>';
  roundSelect.value = [...roundSelect.options].some(o => o.value === before) ? before : String(t.round || '');
  renderEntries();
  await renderAdminRound();
}

async function renderAdminRound() {
  const number = $('#admin-round').value;
  if (!number || !adminTournament) { $('#admin-round-view').innerHTML = '<p class="caption">Vòng đầu tiên sẽ hiện sau khi bấm “Mở vòng”.</p>'; return; }
  const {items, total} = await api(`/tournaments/${adminTournament}/rounds/${number}?limit=300`);
  $('#admin-round-view').innerHTML = items.map(p => `<div class="job"><div><strong>${escape(p.a_name)}</strong> × <strong>${escape(p.b_name || '—')}</strong><p>${!p.b ? 'BYE' : p.forfeit ? `Xử thắng: ${escape(p.forfeit === p.a ? p.b_name : p.a_name)} (đối thủ bị truất quyền)` : p.games.map((g, i) => `${i === 2 ? 'Ván quyết định' : `Ván ${i + 1}`}: ${escape(g.status === 'done' ? outcome(g) : labels[g.status] || g.status)}`).join(' · ')}</p></div></div>`).join('') + (total > items.length ? `<p class="caption">Hiển thị ${items.length}/${total} cặp.</p>` : '') || '<p class="caption">Vòng này chưa có cặp đấu.</p>';
}

function renderEntries() {
  if (!detail) return;
  const running = ['frozen','running'].includes(detail.tournament.status);
  const rows = detail.standings;
  $('#entry-body').innerHTML = rows.length ? rows.map(s => `<tr><td class="rank">${s.rank ?? '—'}</td><td>${escape(s.name)}<small>${escape(s.bot)} · ${escape(labels[s.bot_status] || s.bot_status)}</small></td><td>${escape(s.tier)}</td><td>${s.points}</td><td>${s.wins}/${s.draws}/${s.losses}</td><td>${running && s.entry_status === 'active' ? `<button class="text-link danger" data-dq="${s.team_id}">Truất quyền</button>` : detail.tournament.status === 'open' ? `<button class="text-link danger" data-remove-entry="${s.team_id}">Xóa khỏi giải</button>` : ''}</td></tr>`).join('')
    : '<tr><td colspan="6">Chưa có đội đăng ký.</td></tr>';
}

function syncTournamentForm() {
  const form = $('#tournament-form'), seeded = Boolean(form.elements.source.value), fmt = form.elements.format.value;
  $('#seed-count-field').hidden = !seeded;
  $('#deadline-field').hidden = seeded;
  form.elements.deadline.required = !seeded;
  $('#rounds-field').hidden = fmt !== 'swiss';
  $('#rounds-hint').textContent = 'Gợi ý: 7 vòng đủ chọn top 32 của 1.000 đội; 10–11 vòng để phân hạng toàn bộ.';
  $('#format-help').textContent = FORMAT_HELP[fmt];
  const teams = Math.max(2, Number($('#plan-teams').value) || 2);
  const rounds = fmt === 'swiss' ? Number(form.elements.rounds.value) || 1 : fmt === 'knockout' ? Math.ceil(Math.log2(teams)) : teams % 2 ? teams : teams - 1;
  const games = fmt === 'swiss' ? Math.ceil(teams / 2) * 2 * rounds : fmt === 'knockout' ? (teams - 1) * 2 : teams * (teams - 1);
  const perRound = Math.ceil(teams / 2) * 2;
  $('#plan-preview').textContent = fmt === 'swiss'
    ? `Với ${teams} đội: ${rounds} vòng, khoảng ${perRound} ván mỗi vòng (${games} ván tổng). Máy chấm hiện làm khoảng 4 ván/giây nên mỗi vòng cần chừng ${Math.max(1, Math.ceil(perRound / 4.3 / 60))} phút. Cần tối thiểu ${Math.ceil(Math.log2(teams))} vòng để tìm ra người dẫn đầu rõ ràng.`
    : fmt === 'knockout'
      ? `Với ${teams} đội: ${rounds} vòng (vòng cuối là chung kết), khoảng ${games} ván tổng; mỗi cặp hòa 1–1 có thêm 1 ván quyết định.`
      : `Với ${teams} đội: ${rounds} vòng, ${games} ván tổng. Chỉ phù hợp tối đa 32 đội.`;
}

const FORMAT_HELP = {
  swiss: 'Swiss: mọi đội chơi đủ số vòng, không ai bị loại. Mỗi vòng ghép các đội gần điểm nhau. Hợp nhất cho vòng loại đông người; kết quả cuối là bảng xếp hạng.',
  round_robin: 'Vòng tròn: mỗi đội gặp mọi đội còn lại đúng một lần. Công bằng nhất nhưng số ván tăng rất nhanh, chỉ dùng cho nhóm nhỏ (tối đa 32 đội).',
  knockout: 'Loại trực tiếp: thua cặp là bị loại, vòng cuối là chung kết. Kịch tính, hợp cho top 16/32. Cặp hòa 1–1 đấu thêm ván quyết định. Số đội lẻ thì hạt giống cao nhận BYE.',
};

const PRESETS = {
  qualifier: {format: 'swiss', rounds: 7, map: 'random', final_map: '', source: '', name: 'OTTv2 · Vòng loại tân sinh viên',
    description: 'Mỗi đội chọn một bot trước hạn. Thi 7 vòng Swiss, mỗi cặp hai ván đổi màu.'},
  final: {format: 'knockout', map: 'random', final_map: 'standard', name: 'OTTv2 · Chung kết',
    description: 'Các đội đứng đầu vòng loại đấu loại trực tiếp. Hòa 1–1 đấu thêm ván quyết định; chung kết dùng bản đồ Chuẩn.'},
};

function applyPreset(name) {
  const form = $('#tournament-form');
  for (const [key, value] of Object.entries(PRESETS[name])) if (form.elements[key]) form.elements[key].value = value;
  if (name === 'final') {
    const source = $('#seed-source'), first = [...source.options].find(o => o.value);
    if (first) { source.value = first.value; form.elements.seed_count.value = 16; }
    else notice('Chưa có giải nào hoàn tất để lấy top N. Hoàn tất giải vòng loại trước, hoặc để “Đăng ký mở”.');
  }
  syncTournamentForm();
  form.scrollIntoView({behavior: 'smooth', block: 'start'});
}

// What the organiser should do next, in plain words, from the tournament's state.
function nextAction(t, d) {
  const now = Date.now() / 1000, open = t.status === 'open', unfinished = d.unfinished, last = t.round >= t.rounds;
  const pending = d.pending ? ` Còn ${d.pending} bot đang chờ kiểm tra.` : '';
  if (t.status === 'cancelled') return {step: 0, text: 'Giải đã bị hủy; không còn thao tác nào.', hint: 'Giải đã kết thúc.'};
  if (t.status === 'completed') return {step: 5, text: 'Giải đã kết thúc. Bấm “Tải bảng điểm CSV” để công bố, hoặc tạo giải loại trực tiếp từ top N ở form bên trái.', hint: 'Giải đã hoàn tất.'};
  if (open && t.deadline > now) return {step: 2, text: `Đang nhận đăng ký đến ${formatTime(t.deadline)} (${countdown(t.deadline)}). ${d.count} đội đã đăng ký.${pending} Bạn chưa cần làm gì; muốn khóa sớm hãy mở “Chỉnh sửa giải” và bấm “Đóng đăng ký ngay”.`, hint: 'Chưa mở được: giải còn đang nhận đăng ký.'};
  if (open) return {step: 3, text: `Đã hết hạn đăng ký với ${d.count} đội.${pending} ${d.pending ? 'Chờ kiểm tra xong rồi bấm “Khóa danh sách”, hoặc bấm “Khóa bắt buộc” để loại các bot còn chờ.' : 'Bấm “Khóa danh sách” để chốt và xếp hạt giống.'}`, hint: 'Chưa mở được: hãy khóa danh sách trước.'};
  if (t.status === 'frozen') return {step: 4, text: `Danh sách đã khóa (${d.active} đội thi đấu). Bấm “Mở vòng” để ghép cặp vòng 1.`, hint: 'Sẵn sàng: mở vòng 1.'};
  if (unfinished > 0) return {step: 4, text: `Vòng ${t.round}/${t.rounds} đang chấm: còn ${unfinished} ván. ${t.auto_advance ? 'Hệ thống sẽ tự mở vòng tiếp khi xong.' : 'Khi xong hãy bấm “Mở vòng”.'}`, hint: `Chưa mở được: còn ${unfinished} ván chưa xong.`};
  const ko = t.format === 'knockout';
  return {step: 4, text: last ? `Vòng cuối đã chấm xong. Bấm “Mở vòng / Hoàn tất” để kết thúc giải${ko ? ' (nếu có cặp hòa 1–1, bước này tạo ván quyết định trước)' : ''}.` : `Vòng ${t.round}/${t.rounds} đã xong. Bấm “Mở vòng” để sang vòng ${t.round + 1}${ko ? '; cặp hòa 1–1 sẽ đấu ván quyết định trước' : ''}.`, hint: 'Sẵn sàng: mở vòng tiếp theo.'};
}

function renderGuidance(t, d) {
  const {step, text, hint} = nextAction(t, d);
  for (const li of $$('#stepper li')) { const n = Number(li.dataset.step); li.className = n < step ? 'done' : n === step ? 'current' : ''; if (n === step) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current'); }
  $('#next-action').innerHTML = `<strong>${step ? `Bước ${step}/5 · ` : ''}Việc cần làm tiếp theo</strong><p>${escape(text)}</p>`;
  $('#next-hint').textContent = hint;
}

// ---- people, bots, queue, log -------------------------------------------

async function renderTeams() {
  const {items} = await api(`/admin/teams?q=${encodeURIComponent($('#team-search').value)}`);
  $('#team-list').innerHTML = items.length ? items.map(t => `<div class="job"><div><strong>${escape(t.name)}</strong> ${t.status !== 'active' ? badge('disqualified') : ''}<p>Mã ${escape(t.code)} · ${t.members} thành viên · ${t.valid_bots}/${t.bots} bot hợp lệ · Elo ${t.rating ?? 1000}<br>${escape(t.member_list.map(m => `${m.name} (@${m.username}${m.student_id ? `, ${m.student_id}` : ''})`).join(', ') || t.note || 'Chưa có thành viên')}</p></div><div class="action-row"><button class="button" data-team-action="rename" data-team="${t.id}">Đổi tên</button><button class="button" data-team-action="new_code" data-team="${t.id}">Mã mới</button><button class="button danger" data-team-action="delete" data-team="${t.id}" data-name="${escape(t.name)}">Xóa đội</button>${t.status === 'active' ? `<button class="button danger" data-team-action="disqualify" data-team="${t.id}">Truất quyền</button>` : `<button class="button" data-team-action="reinstate" data-team="${t.id}">Khôi phục</button>`}</div></div>`).join('')
    : '<p class="caption">Không tìm thấy đội.</p>';
}

async function renderUsers() {
  const {items} = await api(`/admin/users?q=${encodeURIComponent($('#user-search').value)}`);
  $('#user-list').innerHTML = items.length ? items.map(u => `<div class="job"><div><strong>${escape(u.name)}</strong> <small>@${escape(u.username)}</small> ${u.role === 'admin' ? '<span class="badge">BTC</span>' : ''} ${u.disabled ? badge('disqualified') : ''}<p>${escape(u.team || 'Chưa có đội')}${u.student_id ? ` · MSSV ${escape(u.student_id)}` : ''} · tạo ${formatTime(u.created)}${u.must_change ? ' · đang dùng mật khẩu tạm' : ''}</p></div><div class="action-row"><button class="button" data-user-action="reset_password" data-user="${u.id}">Cấp lại mật khẩu</button>${u.team ? `<button class="button" data-user-action="remove_from_team" data-user="${u.id}">Rời đội</button>` : ''}<button class="button" data-user-action="${u.disabled ? 'enable' : 'disable'}" data-user="${u.id}">${u.disabled ? 'Mở khóa' : 'Khóa'}</button><button class="button" data-user-action="${u.role === 'admin' ? 'remove_admin' : 'make_admin'}" data-user="${u.id}">${u.role === 'admin' ? 'Bỏ quyền BTC' : 'Cấp quyền BTC'}</button><button class="button danger" data-user-action="delete" data-user="${u.id}" data-username="${escape(u.username)}" data-team-size="${u.team_size || 0}" data-team-name="${escape(u.team || '')}">Xóa tài khoản</button></div></div>`).join('')
    : '<p class="caption">Không tìm thấy tài khoản.</p>';
}

async function renderBots() {
  const {items} = await api(`/admin/submissions?q=${encodeURIComponent($('#bot-search').value)}&status=${$('#bot-status').value}`);
  $('#bot-list').innerHTML = items.length ? items.map(s => `<article class="submission"><div><strong>${escape(s.team)}</strong><small>${escape(s.name)} · ${formatTime(s.created)} · ${s.origin === 'boss' ? 'Boss' : 'Web'} · ${escape(s.hash.slice(0,10))}${s.shared_by > 1 ? ` · <b class="red">trùng mã với ${s.shared_by} đội</b>` : ''}</small></div>${badge(s.status)}<div class="submission-log">${escape(s.log)}</div><div class="submission-actions"><button class="text-link" data-view-source="${s.id}" data-label="${escape(s.team)} · ${escape(s.name)}">Xem mã ↗</button></div></article>`).join('')
    : '<div class="empty">Không có bài nộp phù hợp.</div>';
}

async function renderMatches() {
  const params = new URLSearchParams({q: $('#match-search').value, status: $('#match-status').value, kind: $('#match-kind').value});
  const {items} = await api(`/admin/matches?${params}`);
  $('#match-admin-list').innerHTML = items.length ? items.map(m => `<div class="job"><div>${badge(m.status)} <strong>${escape(m.team1)}</strong> × <strong>${escape(m.team2)}</strong> · ${escape(kinds[m.kind] || m.kind)}<p>${escape(m.bot1)} / ${escape(m.bot2)} · ${formatTime(m.created)}${m.status === 'done' ? ` · ${escape(outcome(m))}` : ''}${m.error ? ` · ${escape(m.error)}` : ''} · <small>${escape(m.id)}</small></p></div>${!m.pairing_id && ['queued','running','failed'].includes(m.status) ? `<button class="button" data-cancel-match="${m.id}">Hủy ván</button>` : ''}</div>`).join('')
    : '<p class="caption">Không có ván phù hợp.</p>';
}

async function renderJobs() {
  const {items} = await api(`/admin/jobs?status=${$('#job-status').value}`);
  $('#job-list').innerHTML = items.length ? items.map(j => `<div class="job"><div>${badge(j.status)} ${j.kind === 'game' ? 'Ván đấu' : 'Kiểm tra bot'} · ưu tiên ${j.priority} · lần ${j.attempts}<p>${escape(j.error || `Tạo ${formatTime(j.created)}${j.started ? ` · bắt đầu ${formatTime(j.started)}` : ''}${j.finished && j.started ? ` · ${duration(j.finished - j.started)}` : ''}`)}</p></div>${j.status === 'failed' ? `<button class="button" data-retry="${j.id}">Thử lại</button>` : ''}</div>`).join('')
    : '<p class="caption">Hàng đợi đang trống.</p>';
}

async function renderAudit() {
  const {items} = await api('/admin/audit');
  $('#audit-list').innerHTML = items.map(a => `<div class="audit-line">${formatTime(a.created)} · <strong>${escape(a.action)}</strong> · ${escape(a.actor)} · ${escape(a.detail)}</div>`).join('');
}

async function refresh() {
  if (!me) return;
  if (tab === 'dashboard') await renderDashboard();
  if (tab === 'events') await renderEvents();
  if (tab === 'people') await Promise.all([renderTeams(), renderUsers()]);
  if (tab === 'bots') await renderBots();
  if (tab === 'matches') await renderMatches();
  if (tab === 'queue') await renderJobs();
  if (tab === 'log') await renderAudit();
}
const refresher = poller(refresh, () => ['dashboard','queue','events'].includes(tab) ? 5000 : 30000);

// ---- events -------------------------------------------------------------

document.addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.close) $(`#${button.dataset.close}`).close();
  if (button.dataset.cancelMatch) action(button, async () => { if (!confirm('Hủy ván này?')) return; await api(`/admin/matches/${button.dataset.cancelMatch}`, {action: 'cancel'}); notice('Đã hủy ván.'); await renderMatches(); });
  if (button.dataset.cancelQueue) action(button, async () => { if (!confirm('Hủy mọi ván đang chờ thuộc loại này?')) return; const r = await api('/admin/matches-cancel-queued', {kind: button.dataset.cancelQueue}); notice(`Đã hủy ${r.cancelled} ván đang chờ.`); await renderMatches(); });
  if (button.dataset.retry) action(button, async () => { await api(`/admin/jobs/${button.dataset.retry}/retry`, {}); notice('Đã đưa job trở lại hàng đợi.'); await renderJobs(); });
  if (button.dataset.removeEntry) action(button, async () => {
    if (!confirm('Xóa đội này khỏi danh sách đăng ký? (Đội có thể đăng ký lại trước hạn.)')) return;
    await api(`/admin/tournaments/${adminTournament}/entries/${button.dataset.removeEntry}/remove`, {});
    notice('Đã xóa đội khỏi giải.'); await renderEvents(); });
  if (button.dataset.dq) action(button, async () => {
    const reason = prompt('Lý do truất quyền (ghi vào nhật ký):'); if (reason === null) return;
    await api(`/admin/tournaments/${adminTournament}/entries/${button.dataset.dq}/disqualify`, {reason});
    notice('Đã truất quyền; các ván chưa đấu của đội được xử thua.'); await renderEvents(); });
  if (button.dataset.teamAction) action(button, async () => {
    const kind = button.dataset.teamAction, body = {action: kind};
    if (kind === 'rename') { body.name = prompt('Tên đội mới:'); if (!body.name) return; }
    if (kind === 'delete') {
      const typed = prompt(`XÓA ĐỘI “${button.dataset.name}”?\nĐội sẽ biến mất cùng toàn bộ bot, ván đấu xếp hạng và tiến độ chiến dịch. Nếu đội đã dự giải, đội chỉ bị ẩn để bảng điểm giải giữ nguyên. Thành viên (nếu còn) được rời đội, tài khoản vẫn giữ.\n\nGõ đúng tên đội để xác nhận:`);
      if (typed === null) return;
      if (typed.trim() !== button.dataset.name) throw new Error('Tên đội không khớp, chưa xóa.');
    }
    if (kind === 'disqualify') { body.note = prompt('Lý do truất quyền đội (áp dụng cho mọi giải đang chạy):'); if (body.note === null) return; }
    const result = await api(`/admin/teams/${button.dataset.team}`, body);
    notice(result.code ? `Mã đội mới: ${result.code}` : kind === 'delete' ? (result.mode === 'hidden' ? 'Đã xóa đội khỏi danh sách (đội từng dự giải nên dữ liệu giải được giữ).' : 'Đã xóa đội cùng toàn bộ bot và ván đấu.') : 'Đã cập nhật đội.');
    await Promise.all([renderTeams(), renderUsers()]); });
  if (button.dataset.userAction) action(button, async () => {
    const kind = button.dataset.userAction, body = {action: kind};
    if (kind === 'delete') {
      const typed = prompt(`XÓA VĨNH VIỄN tài khoản @${button.dataset.username}? Không khôi phục được. Bot và kết quả của đội vẫn được giữ.\n\nGõ tên đăng nhập để xác nhận:`);
      if (typed === null) return;
      if (typed.trim() !== button.dataset.username) throw new Error('Tên đăng nhập không khớp, chưa xóa.');
      if (Number(button.dataset.teamSize) === 1) {
        body.delete_team = confirm(`@${button.dataset.username} là thành viên cuối của đội “${button.dataset.teamName}”.\n\nOK: xóa luôn đội cùng toàn bộ bot và ván đấu của đội.\nHủy: chỉ xóa tài khoản, đội vẫn giữ bot, Elo và kết quả (đội sẽ không còn thành viên).`);
      }
    } else if (['disable','remove_admin','remove_from_team'].includes(kind) && !confirm('Xác nhận thao tác với tài khoản này?')) return;
    const result = await api(`/admin/users/${button.dataset.user}`, body);
    if (result.password) prompt('Mật khẩu tạm (chỉ hiện một lần, gửi riêng cho thí sinh):', result.password);
    else notice(kind === 'delete' ? (result.team_deleted ? 'Đã xóa tài khoản và đội.' : result.team_kept ? 'Đã xóa tài khoản. Đội vẫn giữ bot và kết quả.' : 'Đã xóa tài khoản.') : 'Đã cập nhật tài khoản.');
    await Promise.all([renderUsers(), renderTeams()]); });
  if (button.dataset.viewSource) action(button, async () => {
    const {source} = await api(`/admin/submissions/${button.dataset.viewSource}/source`);
    $('#source-title').textContent = button.dataset.label; $('#source-view').textContent = source; $('#source-dialog').showModal(); });
});

$('#settings-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const form = event.target, values = {announcement: form.elements.announcement.value};
  for (const key of ['registration_open','submissions_open','ranked_open','practice_open','campaign_open','live_open','read_only']) values[key] = form.elements[key].checked;
  if (values.read_only && !confirm('Chế độ chỉ xem chặn mọi thao tác ghi của thí sinh. Bật?')) return;
  await api('/admin/settings', values); notice('Đã lưu cài đặt.'); await renderDashboard(); }); });

$('#tournament-form').addEventListener('change', syncTournamentForm);
$('#tournament-form').addEventListener('input', event => { if (event.target.id === 'plan-teams' || event.target.name === 'rounds') syncTournamentForm(); });
for (const button of $$('[data-preset]')) button.addEventListener('click', () => applyPreset(button.dataset.preset));
$('#tournament-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const form = event.target, data = Object.fromEntries(new FormData(form));
  const body = {name: data.name, format: data.format, rounds: Number(data.rounds), map: data.map, final_map: data.final_map || null, description: data.description, auto_advance: form.elements.auto_advance.checked};
  if (data.source) Object.assign(body, {seed_from: data.source, seed_count: Number(data.seed_count)});
  else body.deadline = new Date(data.deadline).getTime() / 1000;
  const result = await api('/admin/tournaments', body);
  adminTournament = result.id; notice(data.source ? 'Đã tạo vòng tiếp theo từ top đội và khóa danh sách. Bấm “Mở vòng” để bắt đầu.' : 'Đã mở giải. Thí sinh có thể đăng ký bot.');
  await renderEvents(); }); });
$('#tournament-edit').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const form = event.target, t = detail.tournament, body = {name: form.elements.name.value, description: form.elements.description.value};
  if (['open', 'frozen', 'running'].includes(t.status)) { body.map = form.elements.map.value; body.final_map = form.elements.final_map.value || null; }
  if (t.status === 'open' && !t.seed_from) {
    if (form.elements.deadline.value) body.deadline = new Date(form.elements.deadline.value).getTime() / 1000;
    if (t.format === 'swiss' && form.elements.rounds.value) body.rounds = Number(form.elements.rounds.value);
  }
  await api(`/admin/tournaments/${adminTournament}/update`, body); editingFor = '';
  notice('Đã lưu thay đổi.'); await renderEvents(); }); });
$('#close-now').addEventListener('click', event => action(event.target, async () => {
  if (!confirm('Đóng đăng ký ngay? Thí sinh sẽ không đổi bot được nữa; sau đó bấm “Khóa danh sách”.')) return;
  await api(`/admin/tournaments/${adminTournament}/update`, {deadline: Date.now() / 1000}); editingFor = '';
  notice('Đã đóng đăng ký. Bấm “Khóa danh sách” khi các bot đã được kiểm tra.'); await renderEvents(); }));
$('#cancel-tournament').addEventListener('click', event => action(event.target, async () => {
  if (!confirm('HỦY giải này? Mọi ván chưa xong sẽ bị hủy và giải không thể mở lại.')) return;
  const r = await api(`/admin/tournaments/${adminTournament}/cancel`, {});
  notice(`Đã hủy giải (${r.games_cancelled} ván chưa xong bị hủy).`); await renderEvents(); }));
$('#add-entry').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const r = await api(`/admin/tournaments/${adminTournament}/entries`, {team: event.target.elements.team.value});
  event.target.reset(); notice(`Đã thêm đội ${r.team} vào giải.`); await renderEvents(); }); });
$('#admin-round').addEventListener('change', () => action(null, renderAdminRound));
$('#admin-tournament').addEventListener('change', event => { adminTournament = event.target.value; action(null, renderEvents); });
let entryTimer;
$('#entry-search').addEventListener('input', () => { clearTimeout(entryTimer); entryTimer = setTimeout(() => action(null, renderEvents), 300); });
$('#freeze-button').addEventListener('click', event => action(event.target, async () => { const r = await api(`/admin/tournaments/${adminTournament}/freeze`, {}); notice(`Đã khóa: ${r.accepted} đội thi đấu, ${r.excluded} bị loại do bot không hợp lệ.`); await renderEvents(); }));
$('#force-freeze').addEventListener('click', event => action(event.target, async () => { if (!confirm('Loại mọi đội có bot còn đang chờ kiểm tra?')) return; const r = await api(`/admin/tournaments/${adminTournament}/freeze`, {force: true}); notice(`Đã khóa: ${r.accepted} đội thi đấu, ${r.excluded} bị loại.`); await renderEvents(); }));
$('#next-round-button').addEventListener('click', event => action(event.target, async () => { const r = await api(`/admin/tournaments/${adminTournament}/next`, {}); notice(r.completed ? 'Giải đã hoàn tất. Tải bảng điểm CSV để công bố.' : r.deciders ? `Có ${r.deciders} cặp hòa 1–1: đã tạo ván quyết định. Khi chúng xong, bấm “Mở vòng” lần nữa.` : `Đã mở vòng ${r.round} với ${r.pairs} cặp.`); await renderEvents(); }));
$('#auto-toggle').addEventListener('change', event => action(null, async () => { await api(`/admin/tournaments/${adminTournament}/auto`, {value: event.target.checked}); notice(event.target.checked ? 'Giải sẽ tự mở vòng mới khi mọi ván xong.' : 'Đã tắt tự mở vòng.'); await renderEvents(); }));
$('#export-button').addEventListener('click', event => action(event.target, () => download(`/admin/tournaments/${adminTournament}/export`, 'bang-diem.csv')));

let searchTimer;
for (const [input, work] of [['#team-search', renderTeams], ['#user-search', renderUsers], ['#bot-search', renderBots], ['#match-search', renderMatches]]) {
  $(input).addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => action(null, work), 300); });
}
$('#bot-status').addEventListener('change', () => action(null, renderBots));
for (const id of ['#match-status', '#match-kind']) $(id).addEventListener('change', () => action(null, renderMatches));
$('#job-status').addEventListener('change', () => action(null, renderJobs));
$('#cleanup-teams').addEventListener('click', event => action(event.target, async () => {
  const preview = await api('/admin/teams-cleanup', {});
  if (!preview.count) { notice('Không có đội nào trống.'); return; }
  if (!confirm(`Xóa ${preview.count} đội không còn thành viên?\n${preview.names.join(', ')}${preview.count > preview.names.length ? ', …' : ''}\n\nBot, ván đấu và Elo của các đội này sẽ mất (đội từng dự giải chỉ bị ẩn).`)) return;
  const done = await api('/admin/teams-cleanup', {confirm: true});
  notice(`Đã dọn ${done.count} đội (${done.deleted} xóa hẳn, ${done.hidden} chỉ ẩn vì từng dự giải).`);
  await Promise.all([renderTeams(), renderUsers()]); }));
$('#retry-all').addEventListener('click', event => action(event.target, async () => { const r = await api('/admin/jobs/retry-failed', {}); notice(`Đã đưa ${r.count} job lỗi trở lại hàng đợi.`); await renderJobs(); }));

const deadline = new Date(Date.now() + 3600000); deadline.setMinutes(deadline.getMinutes() - deadline.getTimezoneOffset());
$('#deadline').value = deadline.toISOString().slice(0, 16);
syncTournamentForm();

function enter(user) {
  me = user;
  document.body.classList.remove('guest');
  $('#console').hidden = false;
  $('#admin-name').textContent = `${user.name} · @${user.username}`;
  setTab(location.hash.slice(1) || 'dashboard');
}

function leave() {
  me = null;
  document.body.classList.add('guest');
  $('#console').hidden = true;
  $('#admin-name').textContent = '';
}

$('#admin-show-password').addEventListener('click', event => {
  const field = $('#admin-login').elements.password, show = field.type === 'password';
  field.type = show ? 'text' : 'password'; event.target.textContent = show ? 'Ẩn' : 'Hiện'; event.target.setAttribute('aria-pressed', String(show));
});
$('#admin-login').elements.username.addEventListener('input', event => { event.target.value = event.target.value.replace(/\s+/g, '').toLowerCase(); });
$('#admin-login').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, error = $('#admin-error');
  error.textContent = '';
  if (!form.elements.username.value.trim() || !form.elements.password.value) { error.textContent = 'Nhập tên đăng nhập và mật khẩu.'; return; }
  event.submitter.disabled = true;
  try {
    await api('/login', {username: form.elements.username.value.trim().toLowerCase(), password: form.elements.password.value});
    const result = await api('/me');
    session.csrf = result.csrf;
    if (result.user?.role !== 'admin') {
      await api('/logout', {}).catch(() => {});   // a student signing in here should not stay signed in
      session.csrf = null;
      error.textContent = 'Tài khoản này không có quyền ban tổ chức. Hãy dùng tài khoản admin.';
      return;
    }
    form.reset();
    enter(result.user);
  } catch (failure) { error.textContent = failure.message; }
  finally { event.submitter.disabled = false; }
});
$('#admin-logout').addEventListener('click', event => action(event.target, async () => { await api('/logout', {}); session.csrf = null; leave(); }));

try { const result = await api('/me'); session.csrf = result.csrf; result.user?.role === 'admin' ? enter(result.user) : leave(); } catch { leave(); }
