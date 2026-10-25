import {$, $$, escape, labels, formats, kinds, formatTime, badge, session, api, notice, action, options, countdown,
  duration, poller, outcome} from './common.js';

let me = null, tab = 'dashboard', tournaments = [], adminTournament = '', detail = null, importRows = [];

function setTab(value) {
  if (!$$('.view').some(el => el.id === value)) value = 'dashboard';
  tab = value;
  $$('.view').forEach(el => el.hidden = el.id !== tab);
  $$('[role=tab]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.tab === tab)));
  history.replaceState(null, '', `#${tab}`);
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
    for (const key of ['registration_open','submissions_open','ranked_open','practice_open','campaign_open','read_only']) form.elements[key].checked = settings[key] === '1';
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
  const open = tournaments.filter(t => t.status === 'open' && !t.seed_from);
  const target = $('#import-tournament'), selected = target.value;
  target.innerHTML = '<option value="">Không đăng ký vào giải</option>' + open.map(t => `<option value="${escape(t.id)}">${escape(t.name)}</option>`).join('');
  if (open.some(t => t.id === selected)) target.value = selected;
}

async function renderEvents() {
  await loadTournaments();
  if (!adminTournament) { $('#tournament-state').textContent = 'Tạo giải đầu tiên ở bên trái.'; $('#entry-body').innerHTML = ''; return; }
  detail = await api(`/tournaments/${adminTournament}`);
  const t = detail.tournament, active = detail.standings.filter(s => s.entry_status === 'active').length;
  const pending = detail.standings.filter(s => ['queued','running'].includes(s.bot_status)).length;
  $('#tournament-state').innerHTML = `${badge(t.status)} ${escape(formats[t.format])} · Vòng ${t.round}/${t.rounds} · ${detail.standings.length} đăng ký${t.status !== 'open' ? ` · ${active} đang thi` : ''}${pending && t.status === 'open' ? ` · <strong>${pending} bot đang chờ kiểm tra</strong>` : ''}<br>${t.status === 'open' ? `Hạn: ${formatTime(t.deadline)} (${countdown(t.deadline)})` : `${detail.unfinished} ván chưa xong`}`;
  $('#freeze-button').disabled = $('#force-freeze').disabled = t.status !== 'open' || t.deadline > Date.now()/1000;
  $('#next-round-button').disabled = !['frozen','running'].includes(t.status) || detail.unfinished > 0;
  $('#auto-toggle').checked = Boolean(t.auto_advance);
  renderEntries();
}

function renderEntries() {
  if (!detail) return;
  const query = $('#entry-search').value.trim().toLowerCase();
  const running = ['frozen','running'].includes(detail.tournament.status);
  const rows = detail.standings.filter(s => !query || s.name.toLowerCase().includes(query) || s.bot.toLowerCase().includes(query)).slice(0, 300);
  $('#entry-body').innerHTML = rows.length ? rows.map(s => `<tr><td class="rank">${s.rank ?? '—'}</td><td>${escape(s.name)}<small>${escape(s.bot)} · ${escape(labels[s.bot_status] || s.bot_status)}</small></td><td>${escape(s.tier)}</td><td>${s.points}</td><td>${s.wins}/${s.draws}/${s.losses}</td><td>${running && s.entry_status === 'active' ? `<button class="text-link danger" data-dq="${s.team_id}">Truất quyền</button>` : ''}</td></tr>`).join('')
    : '<tr><td colspan="6">Chưa có đội đăng ký.</td></tr>';
}

function syncTournamentForm() {
  const form = $('#tournament-form'), seeded = Boolean(form.elements.source.value), fmt = form.elements.format.value;
  $('#seed-count-field').hidden = !seeded;
  $('#deadline-field').hidden = seeded;
  form.elements.deadline.required = !seeded;
  $('#rounds-field').hidden = fmt !== 'swiss';
  $('#rounds-hint').textContent = 'Gợi ý: 7 vòng đủ chọn top 32 của 1.000 đội; 10–11 vòng để phân hạng toàn bộ.';
}

// ---- import -------------------------------------------------------------

function renderImport(result) {
  importRows = result.rows || result.report || [];
  const s = result.summary || {};
  $('#import-summary').innerHTML = `${result.mapping ? `Cột nhận diện: ${Object.entries(result.mapping).map(([k, v]) => `<code>${escape(k)}</code> = “${escape(v)}”`).join(', ')}<br>` : ''}${s.rows ?? importRows.length} dòng · ${s.ready ?? 0} sẵn sàng · ${s.imported != null ? `${s.imported} đã nhập · ` : ''}${s.unchanged ?? 0} không đổi · ${s.superseded ?? 0} bị thay · <strong>${s.error ?? 0} lỗi</strong>${s.accounts ? ` · ${s.accounts} tài khoản mới` : ''}${s.error && typeof s.error === 'string' ? `<br>${escape(s.error)}` : ''}`;
  renderImportRows();
}

function renderImportRows() {
  const filter = $('#import-filter').value;
  const rows = importRows.filter(r => !filter || r.status === filter);
  const tone = {error:'bad', ready:'', imported:'', superseded:'wait', unchanged:'wait'};
  const names = {error:'Lỗi', ready:'Sẵn sàng', imported:'Đã nhập', superseded:'Bị thay', unchanged:'Không đổi'};
  $('#import-rows').innerHTML = rows.length ? rows.slice(0, 1000).map(r => `<tr><td>${r.row}</td><td>${escape(r.team || r.code)}${r.created_team ? '<small>đội mới</small>' : ''}</td><td><span class="badge ${tone[r.status]}">${escape(names[r.status] || r.status)}</span></td><td>${escape(r.message)}</td></tr>`).join('')
    : '<tr><td colspan="4">Không có dòng phù hợp.</td></tr>';
}

async function renderImportHistory() {
  const {items} = await api('/admin/imports');
  $('#import-history').innerHTML = items.length ? items.map(i => `<div class="job"><div>${badge(i.status === 'done' ? 'done' : i.status === 'failed' ? 'failed' : 'running')} ${formatTime(i.created)}<p>${i.summary.imported ?? '…'} đã nhập · ${i.summary.error ?? 0} lỗi${i.summary.accounts ? ` · ${i.summary.accounts} tài khoản` : ''}</p></div><div class="action-row"><button class="button" data-import="${i.id}">Xem</button>${i.credentials ? `<button class="button primary" data-credentials="${i.id}">Tải tài khoản (1 lần)</button>` : ''}</div></div>`).join('')
    : '<p class="caption">Chưa nhập lần nào.</p>';
}

async function waitImport(id) {
  for (;;) {
    const result = await api(`/admin/imports/${id}`);
    if (result.status !== 'running') { renderImport(result); await renderImportHistory(); return result; }
    $('#import-summary').textContent = 'Đang nhập… (tạo tài khoản mất vài giây mỗi 100 đội)';
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
}

// ---- people, bots, queue, log -------------------------------------------

async function renderTeams() {
  const {items} = await api(`/admin/teams?q=${encodeURIComponent($('#team-search').value)}`);
  $('#team-list').innerHTML = items.length ? items.map(t => `<div class="job"><div><strong>${escape(t.name)}</strong> ${t.status !== 'active' ? badge('disqualified') : ''}<p>Mã ${escape(t.code)} · ${t.members} thành viên · ${t.valid_bots}/${t.bots} bot hợp lệ · Elo ${t.rating ?? 1000}<br>${escape(t.member_list.map(m => `${m.name} (@${m.username}${m.student_id ? `, ${m.student_id}` : ''})`).join(', ') || t.note || 'Chưa có thành viên')}</p></div><div class="action-row"><button class="button" data-team-action="rename" data-team="${t.id}">Đổi tên</button><button class="button" data-team-action="new_code" data-team="${t.id}">Mã mới</button>${t.status === 'active' ? `<button class="button danger" data-team-action="disqualify" data-team="${t.id}">Truất quyền</button>` : `<button class="button" data-team-action="reinstate" data-team="${t.id}">Khôi phục</button>`}</div></div>`).join('')
    : '<p class="caption">Không tìm thấy đội.</p>';
}

async function renderUsers() {
  const {items} = await api(`/admin/users?q=${encodeURIComponent($('#user-search').value)}`);
  $('#user-list').innerHTML = items.length ? items.map(u => `<div class="job"><div><strong>${escape(u.name)}</strong> <small>@${escape(u.username)}</small> ${u.role === 'admin' ? '<span class="badge">BTC</span>' : ''} ${u.disabled ? badge('disqualified') : ''}<p>${escape(u.team || 'Chưa có đội')}${u.student_id ? ` · MSSV ${escape(u.student_id)}` : ''} · tạo ${formatTime(u.created)}${u.must_change ? ' · đang dùng mật khẩu tạm' : ''}</p></div><div class="action-row"><button class="button" data-user-action="reset_password" data-user="${u.id}">Cấp lại mật khẩu</button>${u.team ? `<button class="button" data-user-action="remove_from_team" data-user="${u.id}">Rời đội</button>` : ''}<button class="button" data-user-action="${u.disabled ? 'enable' : 'disable'}" data-user="${u.id}">${u.disabled ? 'Mở khóa' : 'Khóa'}</button><button class="button" data-user-action="${u.role === 'admin' ? 'remove_admin' : 'make_admin'}" data-user="${u.id}">${u.role === 'admin' ? 'Bỏ quyền BTC' : 'Cấp quyền BTC'}</button></div></div>`).join('')
    : '<p class="caption">Không tìm thấy tài khoản.</p>';
}

async function renderBots() {
  const {items} = await api(`/admin/submissions?q=${encodeURIComponent($('#bot-search').value)}&status=${$('#bot-status').value}`);
  $('#bot-list').innerHTML = items.length ? items.map(s => `<article class="submission"><div><strong>${escape(s.team)}</strong><small>${escape(s.name)} · ${formatTime(s.created)} · ${s.origin === 'form' ? 'Google Form' : 'Web'} · ${escape(s.hash.slice(0,10))}${s.shared_by > 1 ? ` · <b class="red">trùng mã với ${s.shared_by} đội</b>` : ''}</small></div>${badge(s.status)}<div class="submission-log">${escape(s.log)}</div><div class="submission-actions"><button class="text-link" data-view-source="${s.id}" data-label="${escape(s.team)} · ${escape(s.name)}">Xem mã ↗</button></div></article>`).join('')
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
  if (tab === 'import') { await loadTournaments(); await renderImportHistory(); }
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
  if (button.dataset.dq) action(button, async () => {
    const reason = prompt('Lý do truất quyền (ghi vào nhật ký):'); if (reason === null) return;
    await api(`/admin/tournaments/${adminTournament}/entries/${button.dataset.dq}/disqualify`, {reason});
    notice('Đã truất quyền; các ván chưa đấu của đội được xử thua.'); await renderEvents(); });
  if (button.dataset.teamAction) action(button, async () => {
    const kind = button.dataset.teamAction, body = {action: kind};
    if (kind === 'rename') { body.name = prompt('Tên đội mới:'); if (!body.name) return; }
    if (kind === 'disqualify') { body.note = prompt('Lý do truất quyền đội (áp dụng cho mọi giải đang chạy):'); if (body.note === null) return; }
    const result = await api(`/admin/teams/${button.dataset.team}`, body);
    notice(result.code ? `Mã đội mới: ${result.code}` : 'Đã cập nhật đội.'); await renderTeams(); });
  if (button.dataset.userAction) action(button, async () => {
    const kind = button.dataset.userAction;
    if (['disable','remove_admin','remove_from_team'].includes(kind) && !confirm('Xác nhận thao tác với tài khoản này?')) return;
    const result = await api(`/admin/users/${button.dataset.user}`, {action: kind});
    if (result.password) prompt('Mật khẩu tạm (chỉ hiện một lần, gửi riêng cho thí sinh):', result.password);
    else notice('Đã cập nhật tài khoản.');
    await renderUsers(); });
  if (button.dataset.viewSource) action(button, async () => {
    const {source} = await api(`/admin/submissions/${button.dataset.viewSource}/source`);
    $('#source-title').textContent = button.dataset.label; $('#source-view').textContent = source; $('#source-dialog').showModal(); });
  if (button.dataset.import) action(button, async () => renderImport(await api(`/admin/imports/${button.dataset.import}`)));
  if (button.dataset.credentials) action(button, async () => { await download(`/admin/imports/${button.dataset.credentials}/credentials`, 'tai-khoan-doi.csv'); notice('Đã tải. File này chứa mật khẩu: lưu riêng, gửi riêng cho từng đội.'); await renderImportHistory(); });
});

$('#settings-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const form = event.target, values = {announcement: form.elements.announcement.value};
  for (const key of ['registration_open','submissions_open','ranked_open','practice_open','campaign_open','read_only']) values[key] = form.elements[key].checked;
  if (values.read_only && !confirm('Chế độ chỉ xem chặn mọi thao tác ghi của thí sinh. Bật?')) return;
  await api('/admin/settings', values); notice('Đã lưu cài đặt.'); await renderDashboard(); }); });

$('#tournament-form').addEventListener('change', syncTournamentForm);
$('#tournament-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const form = event.target, data = Object.fromEntries(new FormData(form));
  const body = {name: data.name, format: data.format, rounds: Number(data.rounds), description: data.description, auto_advance: form.elements.auto_advance.checked};
  if (data.source) Object.assign(body, {seed_from: data.source, seed_count: Number(data.seed_count)});
  else body.deadline = new Date(data.deadline).getTime() / 1000;
  const result = await api('/admin/tournaments', body);
  adminTournament = result.id; notice(data.source ? 'Đã tạo vòng tiếp theo từ top đội và khóa danh sách. Bấm “Mở vòng” để bắt đầu.' : 'Đã mở giải. Thí sinh có thể đăng ký bot.');
  await renderEvents(); }); });
$('#admin-tournament').addEventListener('change', event => { adminTournament = event.target.value; action(null, renderEvents); });
$('#entry-search').addEventListener('input', renderEntries);
$('#freeze-button').addEventListener('click', event => action(event.target, async () => { const r = await api(`/admin/tournaments/${adminTournament}/freeze`, {}); notice(`Đã khóa: ${r.accepted} đội thi đấu, ${r.excluded} bị loại do bot không hợp lệ.`); await renderEvents(); }));
$('#force-freeze').addEventListener('click', event => action(event.target, async () => { if (!confirm('Loại mọi đội có bot còn đang chờ kiểm tra?')) return; const r = await api(`/admin/tournaments/${adminTournament}/freeze`, {force: true}); notice(`Đã khóa: ${r.accepted} đội thi đấu, ${r.excluded} bị loại.`); await renderEvents(); }));
$('#next-round-button').addEventListener('click', event => action(event.target, async () => { const r = await api(`/admin/tournaments/${adminTournament}/next`, {}); notice(r.completed ? 'Giải đã hoàn tất. Tải bảng điểm CSV để công bố.' : `Đã mở vòng ${r.round} với ${r.pairs} cặp.`); await renderEvents(); }));
$('#auto-toggle').addEventListener('change', event => action(null, async () => { await api(`/admin/tournaments/${adminTournament}/auto`, {value: event.target.checked}); notice(event.target.checked ? 'Giải sẽ tự mở vòng mới khi mọi ván xong.' : 'Đã tắt tự mở vòng.'); await renderEvents(); }));
$('#export-button').addEventListener('click', event => action(event.target, () => download(`/admin/tournaments/${adminTournament}/export`, 'bang-diem.csv')));

$('#import-form').addEventListener('submit', event => { event.preventDefault(); action(event.submitter, async () => {
  const apply = event.submitter.value === 'apply', form = new FormData(event.target);
  if (!form.get('csv')?.size && !form.get('files')?.size) throw new Error('Chọn file CSV hoặc ZIP có kèm CSV.');
  if (!form.get('files')?.size) form.delete('files');
  if (!form.get('csv')?.size) form.delete('csv');
  if (apply && !confirm('Ghi các dòng “Sẵn sàng” vào hệ thống?')) return;
  if (apply) form.set('apply', '1');
  $('#import-summary').textContent = apply ? 'Đang tải lên…' : 'Đang phân tích…';
  const result = await api('/admin/import', undefined, {form});
  if (!apply) { renderImport(result); notice('Chạy thử xong. Kiểm tra các dòng lỗi rồi bấm “Nhập thật”.'); return; }
  const done = await waitImport(result.id);
  notice(done.status === 'done' ? `Đã nhập ${done.summary.imported} bài.${done.credentials ? ' Tải file tài khoản ngay: chỉ tải được một lần.' : ''}` : 'Nhập thất bại; xem ghi chú.', done.status !== 'done');
}); });
$('#import-filter').addEventListener('change', renderImportRows);

let searchTimer;
for (const [input, work] of [['#team-search', renderTeams], ['#user-search', renderUsers], ['#bot-search', renderBots], ['#match-search', renderMatches]]) {
  $(input).addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => action(null, work), 300); });
}
$('#bot-status').addEventListener('change', () => action(null, renderBots));
for (const id of ['#match-status', '#match-kind']) $(id).addEventListener('change', () => action(null, renderMatches));
$('#job-status').addEventListener('change', () => action(null, renderJobs));
$('#retry-all').addEventListener('click', event => action(event.target, async () => { const r = await api('/admin/jobs/retry-failed', {}); notice(`Đã đưa ${r.count} job lỗi trở lại hàng đợi.`); await renderJobs(); }));

const deadline = new Date(Date.now() + 3600000); deadline.setMinutes(deadline.getMinutes() - deadline.getTimezoneOffset());
$('#deadline').value = deadline.toISOString().slice(0, 16);
syncTournamentForm();

try { const result = await api('/me'); me = result.user?.role === 'admin' ? result.user : null; session.csrf = result.csrf; } catch { me = null; }
$('#gate').hidden = Boolean(me);
$('#console').hidden = !me;
if (me) { $('#admin-name').textContent = `${me.name} · @${me.username}`; setTab(location.hash.slice(1) || 'dashboard'); }
