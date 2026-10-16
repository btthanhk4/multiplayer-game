import {newGame, applyMove, squareName} from '../rules.js';

const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'Đang chờ',running:'Đang chạy',valid:'Hợp lệ',rejected:'Cần sửa',failed:'Chờ xử lý sự cố',done:'Đã kết thúc',open:'Đang nhận đăng ký',frozen:'Đã khóa bot',completed:'Hoàn tất'};
const formatTime = seconds => new Date(seconds*1000).toLocaleString('vi-VN', {dateStyle:'short',timeStyle:'short'});
const badge = status => `<span class="badge ${['rejected','failed'].includes(status)?'bad':['queued','frozen'].includes(status)?'wait':''}">${escape(labels[status] || status)}</span>`;
let me = null, csrf = null, tab = 'overview', register = false, loading = false;
let tournaments = [], submissions = [], matches = [], selectedTournament = '', adminTournament = '';
const watched = new Map();
let competitionData = null;

async function api(path, data) {
  const response = await fetch(`/api${path}`, {credentials:'same-origin',headers:data === undefined ? {} : {'Content-Type':'application/json','X-CSRF-Token':csrf || ''},
    ...(data === undefined ? {} : {method:'POST',body:JSON.stringify(data)})});
  let result;
  try { result = await response.json(); } catch { throw new Error('Chưa kết nối được máy chủ đấu trường. Mở trang này qua dịch vụ OTTv2 Arena.'); }
  if (!response.ok) throw new Error(result.error || 'Không thực hiện được yêu cầu.');
  return result;
}
function notice(message, error=false) { $('#notice').hidden=false; $('#notice').classList.toggle('error',error); $('#notice').textContent=message; }
function requireLogin() { if (me) return true; $('#auth-dialog').showModal(); return false; }
function setTab(value) {
  if (!['overview','lab','ranked','competition','watch','admin'].includes(value) || value==='admin' && me?.role!=='admin') value='overview';
  tab=value;
  document.querySelectorAll('.view').forEach(el => el.hidden=el.id!==tab);
  document.querySelectorAll('[role=tab]').forEach(el => el.setAttribute('aria-selected', String(el.dataset.tab===tab)));
  history.replaceState(null,'',`#${tab}`);
  refresh();
}
function options(selector, items, placeholder='Chưa có bot hợp lệ') {
  const el=$(selector), before=el.value;
  el.innerHTML=items.length ? items.map(item=>`<option value="${escape(item.id)}">${escape(item.name)}</option>`).join('') : `<option value="">${placeholder}</option>`;
  if (items.some(item=>item.id===before)) el.value=before;
}
function updateAccount() {
  $('#account-button').textContent=me ? `${me.name} · Đăng xuất` : 'Đăng nhập';
  $('#admin-tab').hidden=me?.role!=='admin';
  if (tab==='admin' && me?.role!=='admin') setTab('overview');
}
function tournamentCards() {
  $('#tournament-count').textContent=`${tournaments.length} GIẢI ĐẤU`;
  $('#tournament-list').innerHTML=tournaments.length ? tournaments.map(t=>`<article class="tournament-card"><div class="card-top"><span class="eyebrow">${t.format==='swiss'?'SWISS':'VÒNG TRÒN'}</span>${badge(t.status)}</div><h3>${escape(t.name)}</h3><p class="caption">${t.entrants} bot đăng ký · Vòng ${t.round}/${t.rounds}<br>Hạn chọn bot: ${formatTime(t.deadline)}</p><button class="button" data-tournament="${t.id}">Xem giải / Đăng ký ↗</button></article>`).join('') : '<div class="empty">Giải khai màn đang được chuẩn bị.<br>Bạn có thể viết bot và thử chiến thuật trong Xưởng bot.</div>';
  options('#competition-select',tournaments,'Chưa có giải đấu');
  if (!tournaments.some(t=>t.id===selectedTournament)) selectedTournament=tournaments[0]?.id || '';
  $('#competition-select').value=selectedTournament;
  options('#admin-tournament',tournaments,'Chưa có giải đấu');
  if (!tournaments.some(t=>t.id===adminTournament)) adminTournament=tournaments[0]?.id || '';
  $('#admin-tournament').value=adminTournament;
  const t=tournaments.find(t=>t.id===adminTournament);
  $('#admin-tournament-state').textContent=t ? `${labels[t.status]} · Vòng ${t.round}/${t.rounds} · Hạn: ${formatTime(t.deadline)}` : 'Tạo giải đầu tiên ở bên trái.';
  $('#freeze-button').disabled=!t || t.status!=='open' || t.deadline>Date.now()/1000;
  $('#next-round-button').disabled=!t || !['frozen','running'].includes(t.status);
}
function submissionRows() {
  $('#submission-count').textContent=`${submissions.length} PHIÊN BẢN`;
  $('#submission-list').innerHTML=!me ? '<div class="empty">Đăng nhập để lưu chiến thuật và theo dõi bài nộp của bạn.</div>' : submissions.length ? submissions.map(s=>`<article class="submission"><div><strong>${escape(s.name)}</strong><small>${formatTime(s.created)} · ${escape(s.hash.slice(0,10))}</small></div>${badge(s.status)}<div class="submission-log">${escape(s.log || 'Bài đã được lưu. Chờ máy chạy bot tiếp nhận.')}</div><div class="submission-actions"><button class="text-link" data-source="${s.id}">Mở mã ↗</button>${s.status==='valid'?`<button class="text-link" data-rank-bot="${s.id}">Tìm đối thủ ↗</button>`:''}</div></article>`).join('') : '<div class="empty">Chưa có bài nộp. Bắt đầu bằng một bot mẫu ở phía trên.</div>';
  const valid=submissions.filter(s=>s.status==='valid');
  for (const id of ['#practice-a','#practice-b','#join-submission','#ranked-submission']) options(id,valid);
}
function renderRanked(data) {
  $('#ranked-waiting').textContent=`${data.waiting} người đang tìm trận`;
  const mine=data.mine;
  $('#ranked-mine').textContent=mine ? `${mine.rating} Elo · ${mine.played} cặp đã đấu` : 'Đăng nhập để xem điểm của bạn.';
  const active=data.series.find(series=>series.status==='running');
  $('#ranked-form').hidden=Boolean(mine?.queued || active);
  $('#ranked-cancel').hidden=!mine?.queued;
  $('#ranked-queue').textContent=mine?.queued ? `Đang tìm đối thủ cho ${mine.queued.bot}. Bạn có thể rời trang; bot vẫn ở hàng chờ.` :
    active ? [active.first_status,active.second_status].includes('failed') ? 'Một ván gặp sự cố hạ tầng. Ban tổ chức sẽ xử lý và chạy lại; chưa tính điểm.' :
      `Đã ghép với ${active.a===me.id?active.b_name:active.a_name}. Hai ván đang chờ hoặc đang chạy trên server.` :
    'Chọn bot hợp lệ để tìm một người chơi khác.';
  $('#ranked-standings').innerHTML=data.leaderboard.length ? data.leaderboard.map((row,index)=>
    `<tr><td class="rank">${index+1}</td><td>${escape(row.name)}<small>@${escape(row.username)}</small></td><td><strong>${row.rating}</strong></td><td>${row.played}</td><td>${row.wins} / ${row.draws} / ${row.losses}</td></tr>`).join('') :
    '<tr><td colspan="5">Chưa có cặp trận xếp hạng hoàn thành.</td></tr>';
  $('#ranked-series').innerHTML=data.series.length ? data.series.map(series=>{
    const youAreA=series.a===me?.id;
    const opponent=youAreA?series.b_name:series.a_name;
    const yourPoints=series.a_points==null?'—':youAreA?series.a_points:2-series.a_points;
    const delta=series.rating_change==null?'':(youAreA?series.rating_change:-series.rating_change);
    return `<article class="series-card"><div><span class="eyebrow">${series.status==='done'?'ĐÃ KẾT THÚC':'ĐANG XỬ LÝ'}</span><h3>Bạn × ${escape(opponent)}</h3><p>${escape(youAreA?series.a_bot:series.b_bot)} / ${escape(youAreA?series.b_bot:series.a_bot)}</p><strong>${yourPoints} / 2 điểm ván${delta!==''?` · ${delta>=0?'+':''}${delta} Elo`:''}</strong></div><div class="series-actions"><button class="button" data-watch="${series.first_match}">Xem ván 1</button><button class="button" data-watch="${series.second_match}">Xem ván 2</button></div></article>`;
  }).join('') : '<div class="empty">Chưa có cặp trận. Chọn bot hợp lệ để tìm đối thủ đầu tiên.</div>';
}
async function renderCompetition() {
  if (!selectedTournament) { $('#competition-info').innerHTML='<div class="empty">Chưa có giải đấu.</div>'; $('#join-form').hidden=true; $('#standings-body').innerHTML=''; return; }
  const id=selectedTournament, result=await api(`/tournaments/${id}`);
  if (id!==selectedTournament) return;
  competitionData=result;
  const t=result.tournament;
  $('#competition-info').innerHTML=`<p>${badge(t.status)} <strong>${escape(t.name)}</strong></p><p class="caption">${t.format==='swiss'?'Swiss theo điểm':'Vòng tròn'} · Vòng ${t.round}/${t.rounds} · Hạn chọn bot ${formatTime(t.deadline)}<br>Luật cố định: ${escape(t.rules_hash.slice(0,12))} · Hai ván đổi màu mỗi cặp.</p>`;
  const open=t.status==='open' && t.deadline>Date.now()/1000;
  $('#join-form').hidden=!open;
  const entry=result.standings.find(s=>s.user_id===me?.id);
  $('#entry-status').textContent=entry ? `Bạn đã chọn: ${entry.bot}. Có thể đổi trước hạn.` : 'Chọn một phiên bản đã vượt kiểm tra để giữ suất của bạn.';
  $('#standings-body').innerHTML=result.standings.length ? result.standings.map(s=>`<tr><td class="rank">${s.rank}</td><td>${escape(s.name)}<small>${escape(s.bot)}</small></td><td><strong>${s.points}</strong></td><td>${s.wins} / ${s.draws} / ${s.losses}${s.byes?` · ${s.byes} BYE`:''}</td><td>${s.buchholz}</td><td>${s.game_points}</td></tr>`).join('') : '<tr><td colspan="6">Chưa có thí sinh đăng ký.</td></tr>';
}
function matchDirectory() {
  const filter=$('#match-filter').value, list=matches.filter(m=>filter==='all'||m.status===filter);
  $('#match-list').innerHTML=list.length ? list.map(m=>`<article class="match-row">${badge(m.status)}<p><strong>${escape(m.bot1)}</strong><br>× ${escape(m.bot2)}</p><p class="caption">${m.pairing_id?`Vòng ${m.round}`:'Trận tập'} · ${formatTime(m.created)}</p><button class="button" data-watch="${m.id}">${watched.has(m.id)?'Bỏ xem':m.status==='done'?'Xem replay':'Theo dõi'}</button></article>`).join('') : '<div class="caption">Chưa có ván phù hợp. Các trận sẽ xuất hiện khi BTC mở vòng hoặc bạn tạo trận tập.</div>';
  $('#watch-count').textContent=`${watched.size} / 4 BÀN`;
}
function board(state) {
  return `<div class="mini-board" role="img" aria-label="Bàn cờ 9 nhân 9, ${state.revision} nước">${state.board.flatMap((row,r)=>row.map((p,c)=>{
    const goal=r===0&&c===8?'goal-blue':r===8&&c===0?'goal-red':'';
    const last=state.lastMove&&[state.lastMove.from,state.lastMove.to].some(x=>x[0]===r&&x[1]===c);
    return `<div class="square ${(r+c)%2?'dark':''} ${goal} ${last?'last':''}" title="${squareName(r,c)}${p?` · ${p.owner==='p1'?'Xanh':'Đỏ'} · ${{rock:'Đấm',paper:'Lá',scissors:'Kéo'}[p.type]}`:''}">${p?`<span class="stone ${p.owner}">${{rock:'Đ',paper:'L',scissors:'K'}[p.type]}</span>`:goal?`<small>${squareName(r,c)}</small>`:''}</div>`;
  })).join('')}</div>`;
}
function outcome(match) {
  if (match.status!=='done') return labels[match.status] || match.status;
  const who=match.winner==='draw'?'Ván hòa':`${match.winner==='p1'?'Xanh':'Đỏ'} thắng`;
  const reason=match.reason?.startsWith('technical:')?'Đối thủ lỗi thực thi/nước đi':match.reason?.startsWith('eliminate:')?'Loại hết một loại quân':({goal:'Chạm ô đích',quiet:'80 nửa lượt không ăn quân',repetition:'Lặp thế ba lần',no_legal_moves:'Không còn nước hợp lệ'}[match.reason] || match.reason);
  return `${who} · ${reason}`;
}
function renderWatch(id) {
  const item=watched.get(id), el=document.getElementById(`watch-${id}`);
  if (!item?.data || !el) return;
  const m=item.data, complete=m.status==='done';
  const state=complete?item.states[item.position]:m.progress?.state || newGame();
  el.innerHTML=`<div class="watch-card-head"><strong>${escape(m.bot1)} <span class="red">×</span> ${escape(m.bot2)}</strong><button class="close-button" data-watch="${id}" aria-label="Bỏ xem trận">×</button></div><div class="watch-card-body">${board(state)}<div class="replay-caption">${escape(outcome(m))}<br><span class="caption">${escape(m.player1)} / ${escape(m.player2)} · ${state.revision} nước</span></div>${complete?`<div class="replay-controls"><button data-step="-1" data-id="${id}" aria-label="Nước trước">‹</button><input type="range" min="0" max="${item.states.length-1}" value="${item.position}" data-replay="${id}" aria-label="Chọn nước trong replay"><button data-step="1" data-id="${id}" aria-label="Nước tiếp">›</button><button data-play="${id}" aria-label="Phát hoặc dừng replay">${item.playing?'Ⅱ':'▷'}</button></div><p class="watch-note">Nước ${item.position}/${item.states.length-1}${item.position?` · ${escape(m.replay.moves[item.position-1].notation)}`:' · Bắt đầu'}${m.replay.execution_mode!=='docker'?' · Trận thử nội bộ':''}</p>`:'<p class="watch-note">Bàn chỉ quan sát · Tự cập nhật khi máy chạy trận hoạt động.</p>'}</div>`;
}
async function updateWatched() {
  await Promise.all([...watched.keys()].map(async id=>{
    const data=await api(`/matches/${id}`), item=watched.get(id);
    if (!item) return;
    const signature=`${data.status}:${data.progress?.ply}:${data.replay?.finished}`;
    if (signature===item.signature) return;
    item.data=data; item.signature=signature;
    if (data.replay) {
      const states=[newGame()];
      for (const move of data.replay.moves) {
        const next=applyMove(states.at(-1),move.side,move.from,move.to);
        if (!next.ok) throw new Error('Replay không khớp phiên bản luật. Hãy liên hệ BTC.');
        states.push(next.state);
      }
      item.states=states; item.position=states.length-1;
    }
    renderWatch(id);
  }));
}
async function toggleWatch(id) {
  if (watched.has(id)) { watched.delete(id); document.getElementById(`watch-${id}`)?.remove(); }
  else {
    if (watched.size>=4) throw new Error('Bạn đang xem bốn bàn. Bỏ một bàn để mở trận khác.');
    if (!watched.size) $('#watch-grid').replaceChildren();
    watched.set(id,{position:0,playing:false});
    const card=document.createElement('article'); card.className='watch-card'; card.id=`watch-${id}`; card.textContent='Đang tải bàn cờ…'; $('#watch-grid').append(card);
    try { await updateWatched(); } catch(error) { watched.delete(id); card.remove(); throw error; }
  }
  if (!watched.size) $('#watch-grid').innerHTML='<div class="empty">Chọn một ván ở bên trái để theo dõi hoặc xem lại.</div>';
  matchDirectory();
}
async function renderJobs() {
  const result=await api('/admin/jobs');
  $('#job-list').innerHTML=result.items.length ? result.items.slice(0,30).map(j=>`<div class="job"><div>${badge(j.status)} ${j.kind==='game'?'Ván đấu':'Kiểm tra bot'} · lần ${j.attempts}<p>${escape(j.error || formatTime(j.created))}</p></div>${j.status==='failed'?`<button class="button" data-retry="${j.id}">Thử lại</button>`:''}</div>`).join('') : '<p class="caption">Hàng đợi đang trống.</p>';
  $('#audit-list').innerHTML=result.audit.map(a=>`<div class="audit-line">${formatTime(a.created)} · ${escape(a.action)} · ${escape(a.detail)}</div>`).join('');
}
async function refresh() {
  if (loading) return;
  loading=true;
  try {
    const [health, ts, ms]=await Promise.all([api('/health'),api('/tournaments'),api('/matches')]);
    tournaments=ts.items; matches=ms.items;
    $('#service-status').textContent=health.runner_online?'Đấu trường đang nhận xử lý':'Bài nộp được lưu · Chờ máy chạy bot';
    $('#service-status').classList.toggle('online',health.runner_online);
    $('#service-detail').textContent=health.runner_online&&health.runner_modes.some(m=>m!=='docker')?'Đang thử nội bộ · Chưa chấm giải chính thức':`${health.pending} lượt xử lý đang chờ hoặc đang chạy`;
    tournamentCards();
    if (me) submissions=(await api('/submissions')).items; else submissions=[];
    submissionRows(); matchDirectory();
    if (tab==='ranked') renderRanked(await api('/ranked'));
    if (tab==='competition') await renderCompetition();
    if (tab==='watch') await updateWatched();
    if (tab==='admin'&&me?.role==='admin') await renderJobs();
  } catch(error) {
    $('#service-status').textContent='Chưa kết nối được máy chủ'; $('#service-status').classList.remove('online');
    $('#service-detail').textContent='Thử kết nối lại tự động. Nội dung đang soạn vẫn được giữ.';
    if (!tournaments.length) $('#tournament-list').innerHTML='<div class="empty">Máy chủ đấu trường chưa sẵn sàng.<br>Bạn vẫn có thể đọc sổ tay và soạn bot; nộp bài cần dịch vụ Arena hoạt động.</div>';
    if (tab==='admin') notice(error.message,true);
  } finally { loading=false; }
}
async function action(button, work) {
  if (button) button.disabled=true;
  try { await work(); } catch(error) { notice(error.message,true); }
  finally { if(button) button.disabled=false; }
}

document.addEventListener('click', event=>{
  const button=event.target.closest('button'); if (!button) return;
  if (button.dataset.tab) setTab(button.dataset.tab);
  if (button.dataset.tournament) {selectedTournament=button.dataset.tournament; setTab('competition');}
  if (button.dataset.sample) action(button,async()=>{
    const response=await fetch(`../sdk/examples/${button.dataset.sample}.py`);
    if (!response.ok) throw new Error('Không tải được bot mẫu.');
    $('#source').value=await response.text(); $('#bot-name').value={first:'Nước đầu tiên',hunter:'Thợ săn',guardian:'Người gác'}[button.dataset.sample]; updateSize();
  });
  if (button.dataset.source) action(button,async()=>{ const result=await api(`/submissions/${button.dataset.source}/source`); $('#source').value=result.source; updateSize(); $('#source').focus(); });
  if (button.dataset.rankBot) { setTab('ranked'); $('#ranked-submission').value=button.dataset.rankBot; }
  if (button.dataset.watch) action(button,()=>toggleWatch(button.dataset.watch));
  if (button.dataset.step) {const item=watched.get(button.dataset.id);item.position=Math.max(0,Math.min(item.states.length-1,item.position+Number(button.dataset.step)));item.playing=false;renderWatch(button.dataset.id);}
  if (button.dataset.play) {const item=watched.get(button.dataset.play);if(item.position===item.states.length-1)item.position=0;item.playing=!item.playing;renderWatch(button.dataset.play);}
  if (button.dataset.retry) action(button,async()=>{await api(`/admin/jobs/${button.dataset.retry}/retry`,{});notice('Đã đưa job trở lại hàng đợi.');await refresh();});
});
$('#competition-select').addEventListener('change',event=>{selectedTournament=event.target.value;action(null,renderCompetition);});
$('#admin-tournament').addEventListener('change',event=>{adminTournament=event.target.value;tournamentCards();});
$('#match-filter').addEventListener('change',matchDirectory);
document.addEventListener('input',event=>{if(event.target.dataset.replay){const id=event.target.dataset.replay,item=watched.get(id);item.position=Number(event.target.value);item.playing=false; const card=document.getElementById(`watch-${id}`);card.querySelector('.mini-board').outerHTML=board(item.states[item.position]);card.querySelector('.watch-note').textContent=`Nước ${item.position}/${item.states.length-1}`;}});
document.addEventListener('change',event=>{if(event.target.dataset.replay)renderWatch(event.target.dataset.replay);});
function updateSize(){ $('#source-size').textContent=`${new TextEncoder().encode($('#source').value).length.toLocaleString('vi-VN')} B`; }
$('#source').addEventListener('input',updateSize);
$('#source').addEventListener('keydown',event=>{if(event.key==='Tab'){event.preventDefault();const el=event.target;el.setRangeText('    ',el.selectionStart,el.selectionEnd,'end');updateSize();}});
$('#source-file').addEventListener('change',()=>action(null,async()=>{const file=$('#source-file').files[0];if(!file)return;if(file.size>65536)throw new Error('File vượt quá 64 KiB.');$('#source').value=await file.text();$('#bot-name').value=file.name.replace(/\.py$/i,'');updateSize();}));
$('#submit-form').addEventListener('submit',event=>{event.preventDefault();if(!requireLogin())return;action(event.submitter,async()=>{await api('/submissions',{name:$('#bot-name').value,source:$('#source').value.replace(/\r\n/g,'\n')});notice('Đã lưu phiên bản. Kết quả kiểm tra sẽ cập nhật trong danh sách bên dưới.');await refresh();});});
$('#practice-form').addEventListener('submit',event=>{event.preventDefault();if(!requireLogin())return;action(event.submitter,async()=>{const result=await api('/practice',{p1:$('#practice-a').value,p2:$('#practice-b').value});notice('Đã xếp hai ván đổi màu vào hàng đợi.');setTab('watch');await toggleWatch(result.ids[0]);});});
$('#ranked-form').addEventListener('submit',event=>{event.preventDefault();if(!requireLogin())return;action(event.submitter,async()=>{const result=await api('/ranked/queue',{submission_id:$('#ranked-submission').value});notice(result.status==='matched'?'Đã ghép đối thủ. Hai ván đổi màu được xếp vào hàng đợi.':'Đã vào hàng chờ. Hệ thống sẽ tự ghép khi có đối thủ.');await refresh();});});
$('#ranked-cancel').addEventListener('click',event=>action(event.target,async()=>{const result=await api('/ranked/cancel',{});notice(result.cancelled?'Đã hủy tìm trận.':'Đối thủ vừa được ghép; cặp trận đã bắt đầu.');await refresh();}));
$('#join-form').addEventListener('submit',event=>{event.preventDefault();if(!requireLogin())return;action(event.submitter,async()=>{await api(`/tournaments/${selectedTournament}/join`,{submission_id:$('#join-submission').value});notice('Đã lưu phiên bản thi đấu.');await refresh();});});
$('#tournament-form').addEventListener('submit',event=>{event.preventDefault();action(event.submitter,async()=>{const data=Object.fromEntries(new FormData(event.target));data.rounds=Number(data.rounds);data.deadline=new Date(data.deadline).getTime()/1000;const result=await api('/admin/tournaments',data);adminTournament=selectedTournament=result.id;notice('Đã mở giải mới. Thí sinh có thể đăng ký bot hợp lệ.');await refresh();});});
$('#freeze-button').addEventListener('click',event=>action(event.target,async()=>{await api(`/admin/tournaments/${adminTournament}/freeze`,{});notice('Đã khóa danh sách và mã băm của các bot.');await refresh();}));
$('#next-round-button').addEventListener('click',event=>action(event.target,async()=>{await api(`/admin/tournaments/${adminTournament}/next`,{});notice('Đã cập nhật vòng đấu.');await refresh();}));
$('#account-button').addEventListener('click',()=>action(null,async()=>{if(me){await api('/logout',{});me=csrf=null;updateAccount();await refresh();}else $('#auth-dialog').showModal();}));
$('#close-auth').addEventListener('click',()=>$('#auth-dialog').close());
$('#toggle-auth').addEventListener('click',()=>{register=!register;$('#register-fields').hidden=!register;$('#auth-form').elements.name.required=register;$('#auth-form').elements.password.minLength=register?12:1;$('#auth-form').elements.password.autocomplete=register?'new-password':'current-password';$('#auth-title').textContent=register?'Đặt tên cho chiến thuật.':'Trở lại bàn cờ.';$('#auth-submit').textContent=register?'Tạo tài khoản':'Đăng nhập';$('#toggle-auth').textContent=register?'Đã có tài khoản? Đăng nhập':'Chưa có tài khoản? Đăng ký';$('#auth-error').textContent='';});
$('#auth-form').addEventListener('submit',async event=>{event.preventDefault();event.submitter.disabled=true;$('#auth-error').textContent='';try{const result=await api(register?'/register':'/login',Object.fromEntries(new FormData(event.target)));me=result.user;csrf=result.csrf;event.target.elements.password.value='';$('#auth-dialog').close();updateAccount();await refresh();}catch(error){$('#auth-error').textContent=error.message;}finally{event.submitter.disabled=false;}});
const deadline=new Date(Date.now()+3600000);deadline.setMinutes(deadline.getMinutes()-deadline.getTimezoneOffset());$('#deadline').value=deadline.toISOString().slice(0,16);
$('#source').value="def choose_move(observation):\n    # Thay chiến thuật của bạn tại đây.\n    move = observation['legalMoves'][0]\n    return {'from': move['from'], 'to': move['to']}\n";updateSize();
try { const sample=await fetch('../sdk/examples/first.py'); if(sample.ok){$('#source').value=await sample.text();$('#bot-name').value='Nước đầu tiên';updateSize();} } catch {}
try {const result=await api('/me');me=result.user;csrf=result.csrf;}catch{}
updateAccount();setTab(location.hash.slice(1)||'overview');
function scheduleRefresh(){setTimeout(()=>{if(!document.hidden)refresh();scheduleRefresh();},tab==='ranked'||tab==='watch'?7000:20000);}
scheduleRefresh();
setInterval(()=>{if(document.hidden||tab!=='watch')return;for(const [id,item] of watched){if(item.playing){item.position=Math.min(item.position+1,item.states.length-1);if(item.position===item.states.length-1)item.playing=false;renderWatch(id);}}},650);
