// Self-contained browser smoke. Uses disposable artifacts/ data and loopback only.
// PowerShell: $env:ARENA_PYTHON='.venv/Scripts/python.exe'; node scripts/arena-smoke.mjs
// Linux: ARENA_PYTHON=.venv/bin/python node scripts/arena-smoke.mjs
import {chromium} from 'playwright';
import {spawn,spawnSync} from 'node:child_process';
import {mkdirSync,mkdtempSync} from 'node:fs';
import {resolve} from 'node:path';
import {randomBytes} from 'node:crypto';
import assert from 'node:assert/strict';
const python=process.env.ARENA_PYTHON || (process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python');
mkdirSync('artifacts',{recursive:true});
const data=mkdtempSync(resolve('artifacts/arena-smoke-'));
const port=18000+Math.floor(Math.random()*10000), base=`http://localhost:${port}`;
const password=randomBytes(24).toString('hex');
const env={...process.env,PYTHONUTF8:'1',ARENA_ORIGIN:base,ARENA_REGISTRATION_CODE:'',ARENA_TEST_PASSWORD:password};
const fixture=spawnSync(python,['scripts/arena-fixture.py',data],{env,encoding:'utf8'});
assert.equal(fixture.status,0,fixture.stderr);
const services=[spawn(python,['-m','arena_backend','--data',data,'serve','--port',String(port)],{env,stdio:'pipe'}),
                spawn(python,['-m','arena_backend','--data',data,'worker','--mode','samples','--slots','2'],{env,stdio:'pipe'})];
let browser;
const errors=[];
const noOverflow=async page=>assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'No horizontal overflow');
async function newPage(width=1440){
  const context=await browser.newContext({viewport:{width,height:1000}});
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
  return page;
}
async function register(page,username,name){
  await page.goto(base+'/');
  await page.locator('#tab-register').click();
  await page.locator('#auth-form [name=username]').fill(username);
  await page.locator('#auth-form [name=password]').fill(password);
  await page.locator('#auth-form [name=name]').fill(name);
  await page.locator('#auth-submit').click(); await page.locator('#gate').waitFor({state:'hidden'});
  await page.locator('#create-team-form').waitFor();
}
try {
  for (let i=0;i<80;i++) {try{if((await fetch(base+'/api/health')).ok)break;}catch{} await new Promise(r=>setTimeout(r,250));}
  browser=await chromium.launch(process.platform==='win32'?{executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true}:{headless:true});
  const page=await newPage();
  await page.goto(base+'/');
  await page.locator('#gate').waitFor();
  assert.equal(await page.locator('main').isVisible(),false,'guests see only the sign-in gate');
  assert.equal((await page.request.get(base+'/api/tournaments')).status(),401);
  assert.equal((await page.request.get(base+'/api/feed')).status(),401);
  assert.equal((await page.request.get(base+'/api/health')).status(),200);
  await page.screenshot({path:'artifacts/arena-gate.png'});
  // Sign-in corner cases: reached through a different address than ARENA_ORIGIN (127.0.0.1 vs localhost).
  const alt=await newPage(1100);
  await alt.goto(base.replace('localhost','127.0.0.1')+'/');
  await alt.locator('#tab-register').click();
  await alt.locator('#auth-form [name=name]').fill('Nguyễn Đức Anh');
  assert.equal(await alt.locator('#auth-form [name=username]').inputValue(),'nguyenducanh','username suggested from name');
  await alt.locator('#auth-form [name=username]').fill('  Alt_USER ');
  assert.equal(await alt.locator('#auth-form [name=username]').inputValue(),'alt_user','username lowercased, spaces removed');
  await alt.locator('#auth-form [name=password]').fill('short');
  await alt.locator('#auth-submit').click();
  await alt.locator('#auth-form [name=password]:invalid').waitFor();
  await alt.locator('#auth-form [name=password]').fill(password);
  await alt.locator('#show-password').click();
  assert.equal(await alt.locator('#auth-form [name=password]').getAttribute('type'),'text');
  await alt.locator('#auth-submit').click(); await alt.locator('#gate').waitFor({state:'hidden'});
  await alt.reload();
  await alt.locator('#create-team-form').waitFor();
  assert.equal(await alt.locator('#gate').isVisible(),false,'session survives a reload over a second address');
  await alt.locator('#account-button').click(); await alt.locator('#logout').click();
  await alt.locator('#gate').waitFor();
  await alt.locator('#auth-form [name=username]').fill('alt_user');
  await alt.locator('#auth-form [name=password]').fill('wrong-password-1');
  await alt.locator('#auth-submit').click();
  await alt.locator('#auth-error').filter({hasText:'không đúng'}).waitFor();
  await alt.locator('#auth-form [name=password]').fill(password);
  await alt.locator('#auth-submit').click(); await alt.locator('#gate').waitFor({state:'hidden'});
  await alt.screenshot({path:'artifacts/arena-signin-alt-origin.png'});
  await register(page,'browser_player','Người viết chiến thuật');
  await page.locator('#create-team-form [name=name]').fill('Kéo Thần Tốc');
  await page.locator('#create-team-form button').click();
  await page.locator('#team-detail').waitFor();
  const code=(await page.locator('#team-code').textContent()).trim();
  assert.match(code,/^[A-Z0-9]{8}$/);
  const mate=await newPage(390);
  await register(mate,'browser_mate','Bạn cùng đội');
  await mate.locator('#join-team-form [name=code]').fill(code.toLowerCase());
  await mate.locator('#join-team-form button').click();
  await mate.locator('#member-count').filter({hasText:'2 /'}).waitFor();
  await noOverflow(mate);
  await mate.screenshot({path:'artifacts/arena-team-mobile.png',fullPage:true});
  await page.locator('[role=tab][data-tab=lab]').click();
  await page.locator('[data-sample=hunter]').click();
  await page.locator('#submit-form button[type=submit]').click();
  await page.locator('.submission .badge').filter({hasText:'Hợp lệ'}).waitFor({timeout:90000});
  await page.screenshot({path:'artifacts/arena-lab.png',fullPage:true});
  await page.locator('.submission [data-rank-bot]').first().click();
  await page.locator('#ranked-form button[type=submit]').click();
  await page.locator('#ranked-cancel').waitFor({state:'visible'});
  await page.locator('#ranked-cancel').click();
  await page.locator('#ranked-form').waitFor({state:'visible'});
  await page.locator('[role=tab][data-tab=campaign]').click();
  await page.locator('#boss-path .boss.open').first().waitFor();
  assert.equal(await page.locator('#boss-path .boss.locked').count(),4);
  await page.locator('#campaign-go:not([disabled])').waitFor({timeout:90000});
  await page.locator('#campaign-go').click();
  await page.locator('#campaign-runs .stars').first().waitFor({timeout:120000});
  await page.locator('#boss-path .boss.cleared').first().waitFor();
  await page.locator('#boss-path .boss.open').filter({hasText:'Thợ săn'}).waitFor();
  await page.screenshot({path:'artifacts/arena-campaign.png',fullPage:true});
  await mate.locator('[role=tab][data-tab=campaign]').click();
  await mate.locator('#boss-path .boss.cleared').first().waitFor();
  await noOverflow(mate);
  await mate.screenshot({path:'artifacts/arena-campaign-mobile.png',fullPage:true});
  // Practice on another map: the replay records and names it.
  await page.locator('[role=tab][data-tab=lab]').click();
  await page.locator('#practice-map').selectOption('wall');
  await page.locator('#practice-form button[type=submit]').click();
  await page.locator('#watch-grid .replay-caption').filter({hasText:'Bản đồ Bức tường'}).waitFor({timeout:60000});
  await page.locator('#watch-grid .watch-card [data-watch]').first().click();
  await page.locator('[role=tab][data-tab=competition]').click();
  await page.locator('#join-form button[type=submit]').click();
  await page.locator('#entry-status').filter({hasText:'Đội đã chọn'}).waitFor();
  await page.locator('[role=tab][data-tab=overview]').click();
  await page.locator('#next-step .progress-dots li.done').nth(3).waitFor();
  assert.equal(await page.locator('#gate').evaluate(el=>getComputedStyle(el).display),'none','sign-in gate never shows to a signed-in user');
  assert.equal(await page.locator('#dashboard-cards .tile').count(),4);
  await page.locator('#feed li').filter({hasText:'Boss 1'}).first().waitFor({timeout:30000});
  await page.screenshot({path:'artifacts/arena-dashboard.png',fullPage:true});
  await page.locator('[role=tab][data-tab=team]').click();
  await page.locator('#team-badges .achievement.earned').nth(2).waitFor();
  await page.locator('[role=tab][data-tab=overview]').click();
  await page.locator('#feed .team-link').first().click();
  await page.locator('#team-dialog[open] h2').filter({hasText:'Kéo Thần Tốc'}).waitFor();
  await page.screenshot({path:'artifacts/arena-profile.png'});
  await page.locator('#team-dialog [data-close=team-dialog]').click();
  assert.equal((await page.request.get(base+'/api/admin/overview')).status(),403);
  await page.locator('[role=tab][data-tab=watch]').click();
  await page.locator('#match-filter').selectOption('all');
  await page.locator('#match-list [data-watch]').nth(2).waitFor();
  // The team's own running games open by themselves on this tab; wait for them to finish and close what opened.
  await page.waitForFunction(()=>!document.querySelector('#match-list .is-live')&&document.querySelector('#live-banner').hidden,null,{timeout:90000});
  while (await page.locator('#watch-grid .watch-card [data-watch]').count()) await page.locator('#watch-grid .watch-card [data-watch]').first().click();
  // Pick three distinct finished games by id (the list may reorder while games start and finish).
  const wanted=(await page.locator('#match-list .match-row:not(.is-live) [data-watch]').evaluateAll(els=>els.slice(0,3).map(e=>e.dataset.watch)));
  assert.equal(new Set(wanted).size,3);
  for(const [i,id] of wanted.slice(0,2).entries()){
    await page.locator(`#match-list [data-watch="${id}"]`).click();
    await page.locator('#watch-grid .watch-card .mini-board').nth(i).waitFor();
  }
  await page.locator(`#match-list [data-watch="${wanted[2]}"]`).click();
  assert.equal(await page.locator('#watch-grid .watch-card').count(),2);
  assert.match(await page.locator('#notice').textContent(),/hai bàn/);
  const slider=page.locator('[data-replay]').first();
  await slider.fill('0');await slider.dispatchEvent('change');
  assert.match(await page.locator('.watch-note').first().textContent(),/Nước 0/);
  await page.locator('[data-speed]').first().click();
  assert.equal(await page.locator('[data-speed]').first().textContent(),'2×');
  await page.locator('[data-expand]').first().click();
  await page.locator('.watch-card.large .material').waitFor();
  await page.setViewportSize({width:390,height:844});
  await noOverflow(page);
  await page.screenshot({path:'artifacts/arena-watch-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});

  // Live spectating: replay a real finished game as if it were running, then finish it.
  while (await page.locator('#watch-grid .watch-card [data-watch]').count()) await page.locator('#watch-grid .watch-card [data-watch]').first().click();
  const listing=(await (await page.request.get(base+'/api/matches?status=done')).json()).items.sort((a,b)=>(b.plies||0)-(a.plies||0));
  const target=listing[0].id, real=await (await page.request.get(base+`/api/matches/${target}`)).json();
  const compact=real.replay.moves.map(m=>[...m.from,...m.to]);
  assert.ok(compact.length>=6,'need a game with a few moves');
  const first=Math.max(2,Math.floor(compact.length/3)), second=Math.max(first+1,Math.floor(compact.length*2/3));
  let phase=0, sinceSeen=[];
  await page.route(new RegExp(`/api/matches/${target}$`),route=>phase<2
    ? route.fulfill({json:{...real,status:'running',replay:null,winner:null,reason:null,progress:{ply:first,thinking:'p2',moves:compact.slice(0,first),left:{p1:58.1,p2:59}},viewers:2,position:null}})
    : route.continue());
  await page.route(new RegExp(`/api/matches/${target}/live`),route=>{
    sinceSeen.push(new URL(route.request().url()).searchParams.get('since'));
    if(phase===0){phase=1;return route.fulfill({json:{status:'running',ply:second,reset:false,moves:compact.slice(first,second),thinking:'p1',left:{p1:57,p2:58.5},viewers:3,position:null,winner:null,reason:null}});}
    phase=2;return route.fulfill({json:{status:'done',ply:compact.length,reset:false,moves:[],thinking:null,left:null,viewers:3,position:null,winner:real.winner,reason:real.reason}});
  });
  await page.locator('[role=tab][data-tab=watch]').click();
  await page.locator('#match-filter').selectOption('all');
  await page.locator(`#match-list [data-watch="${target}"]`).click();
  await page.locator('#watch-grid .live-pill').filter({hasText:'TRỰC TIẾP'}).waitFor();
  await page.locator('.live-strip').filter({hasText:`nước ${second}`}).waitFor({timeout:15000});   // the poll delivered the next batch
  assert.match(await page.locator('.live-strip').textContent(),/3 người đang xem/);
  assert.equal(sinceSeen[0],String(first),'incremental poll asks only for moves after the ones it already has');
  await page.locator('.result-banner').waitFor({timeout:15000});
  assert.equal(await page.locator('[data-golive]').isHidden(),true,'no live button once the game is over');
  assert.equal(await page.locator('[data-r=count]').textContent(),String(compact.length),'final replay replaces the partial move list');
  await page.locator('[data-r=moves] button[data-ply="3"]').click();
  assert.match(await page.locator('.watch-note').textContent(),/Nước 3\//);
  const flippedBefore=await page.locator('.mini-board.flipped').count();
  await page.locator('[data-flip]').click();
  assert.equal(await page.locator('.mini-board.flipped').count(),flippedBefore?0:1,'flip toggles the board orientation');
  await page.keyboard.press('Home');
  assert.match(await page.locator('.watch-note').textContent(),/Nước 0\//);
  await page.keyboard.press('End');
  assert.match(await page.locator('.watch-note').textContent(),new RegExp(`Nước ${compact.length}/`));
  if(real.sibling) await page.locator('[data-sibling]').waitFor();
  await page.screenshot({path:'artifacts/arena-watch-live.png',fullPage:true});
  await page.unroute(new RegExp(`/api/matches/${target}$`));
  await page.unroute(new RegExp(`/api/matches/${target}/live`));

  // ---- sign-in resilience (stale cache, blocked scripts, no JavaScript) ----
  const asked=[];
  const probe=await newPage(1100);
  probe.on('request',r=>{const u=new URL(r.url()); if(/\.(js|css)$/.test(u.pathname)&&u.pathname.startsWith('/arena/')||u.pathname==='/rules.js')asked.push(u.pathname+u.search);});
  await probe.goto(base+'/arena/admin');
  await probe.locator('#admin-login').waitFor();
  assert.ok(asked.length>=4,'admin page loads its modules: '+asked.join(','));
  assert.ok(asked.every(u=>/\?v=[0-9a-f]{10}$/.test(u)),'every script and stylesheet request carries the build version: '+asked.join(','));
  await probe.waitForTimeout(5600);
  assert.equal(await probe.locator('#boot-note').count(),0,'the "page still loading" note is removed once the scripts run');
  // A CDN holding an OLD unversioned admin.js must no longer matter.
  await probe.route(/\/arena\/admin\.js$/,route=>route.fulfill({contentType:'text/javascript',body:'/* stale copy that does nothing */'}));
  await probe.reload();
  await probe.locator('#admin-login [name=username]').fill('arena_admin');
  await probe.locator('#admin-login [name=password]').fill(password);
  await probe.locator('#admin-show-password').click();
  assert.equal(await probe.locator('#admin-login [name=password]').getAttribute('type'),'text','show password works');
  await probe.locator('#admin-show-password').click();
  assert.equal(await probe.locator('#admin-login [name=password]').getAttribute('type'),'password','hide password works');
  await probe.locator('#admin-submit').click();
  await probe.locator('#stats .stat').first().waitFor();
  assert.ok(!probe.url().includes('password='),'password never reaches the address bar');
  await probe.locator('#admin-logout').click();
  await probe.locator('#admin-login').waitFor();
  // Script blocked entirely: the form must not leak the password, and the page must say what is wrong.
  const blocked=await newPage(1100);
  await blocked.route(/\/arena\/(admin|common)\.js/,route=>route.abort());
  await blocked.goto(base+'/arena/admin');
  await blocked.locator('#admin-login [name=username]').fill('arena_admin');
  await blocked.locator('#admin-login [name=password]').fill('very-secret-password');
  await blocked.waitForTimeout(5600);
  assert.equal(await blocked.locator('#boot-note').evaluate(el=>getComputedStyle(el).opacity),'1','the note appears when scripts did not start');
  await blocked.locator('#admin-submit').click();
  await blocked.waitForURL(/enable-javascript/);
  assert.ok(!blocked.url().includes('secret'),'no password in the URL when scripts are blocked');
  assert.match(await blocked.locator('body').textContent(),/chưa được gửi đi/);
  const plain=await browser.newContext({javaScriptEnabled:false}), noscript=await plain.newPage();
  for(const [path,form,user,pass] of [['/','#auth-form','x_user','very-secret-password'],['/arena/admin','#admin-login','x_user','very-secret-password']]){
    await noscript.goto(base+path);
    await noscript.locator(`${form} [name=username]`).fill(user);
    await noscript.locator(`${form} [name=password]`).fill(pass);
    await noscript.locator(`${form} button.primary`).click();
    await noscript.waitForURL(/enable-javascript/);
    assert.ok(!noscript.url().includes('secret')&&!noscript.url().includes('?'),'no query string, no password: '+noscript.url());
  }
  await plain.close();

  const admin=await newPage();
  await admin.goto(base+'/arena/admin');
  await admin.locator('#admin-login').waitFor();
  assert.equal(await admin.locator('#console').isVisible(),false,'the console stays hidden until an admin signs in');
  assert.equal((await admin.request.get(base+'/api/admin/overview')).status(),401);
  await admin.screenshot({path:'artifacts/admin-gate.png'});
  // A student account is turned away and not left signed in.
  await admin.locator('#admin-login [name=username]').fill('browser_player');
  await admin.locator('#admin-login [name=password]').fill(password);
  await admin.locator('#admin-submit').click();
  await admin.locator('#admin-error').filter({hasText:'không có quyền'}).waitFor();
  assert.equal((await (await admin.request.get(base+'/api/me')).json()).user,null);
  await admin.locator('#admin-login [name=username]').fill('arena_admin');
  await admin.locator('#admin-login [name=password]').fill('wrong-password-1');
  await admin.locator('#admin-submit').click();
  await admin.locator('#admin-error').filter({hasText:'không đúng'}).waitFor();
  await admin.locator('#admin-login [name=password]').fill(password);
  await admin.locator('#admin-submit').click();
  await admin.locator('#stats .stat').first().waitFor();
  assert.equal(await admin.locator('#gate').evaluate(el=>getComputedStyle(el).display),'none');
  await admin.reload();
  await admin.locator('#stats .stat').first().waitFor();
  await admin.screenshot({path:'artifacts/admin-dashboard.png',fullPage:true});
  await admin.locator('[role=tab][data-tab=matches]').click();
  await admin.locator('#match-admin-list .job').first().waitFor();
  await admin.locator('#match-kind').selectOption('campaign');
  await admin.locator('#match-admin-list .job').filter({hasText:'Chiến dịch'}).first().waitFor();
  await admin.screenshot({path:'artifacts/admin-matches.png',fullPage:true});
  await admin.locator('[role=tab][data-tab=events]').click();
  await admin.locator('#tournament-form [name=name]').fill('Giải trình duyệt');
  await admin.locator('#tournament-form [name=map]').selectOption('flank');
  await admin.locator('#tournament-form button.primary').click();
  await admin.locator('#admin-tournament').getByRole('option',{name:/Giải trình duyệt/}).waitFor({state:'attached'});
  assert.equal(await admin.locator('#freeze-button').isDisabled(),true);
  await admin.locator('#tournament-state').filter({hasText:'Bản đồ Hai cánh'}).waitFor();
  await admin.locator('#tournament-edit [name=map]').selectOption('block');
  await admin.locator('#tournament-edit [name=name]').fill('Giải đã đổi tên');
  await admin.locator('#tournament-edit button.primary').click();
  await admin.locator('#admin-tournament option').filter({hasText:'Giải đã đổi tên'}).waitFor({state:'attached'});
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#close-now').click();
  await admin.locator('#freeze-button:not([disabled])').waitFor({timeout:15000});
  await admin.locator('#add-entry [name=team]').fill('Không có đội này');
  await admin.locator('#add-entry button').click();
  await admin.locator('#notice.error').waitFor();
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#cancel-tournament').click();
  await admin.locator('#admin-tournament option').filter({hasText:'Đã hủy'}).waitFor({state:'attached'});
  await admin.locator('#cancel-tournament:disabled').waitFor({timeout:15000});
  await admin.screenshot({path:'artifacts/admin-events.png',fullPage:true});
  await admin.locator('[role=tab][data-tab=people]').click();
  await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).waitFor();
  // Deleting an account: needs the typed username, signs the person out, frees the name.
  const doomed=await newPage(1100);
  await register(doomed,'to_delete','Người bị xóa');
  await admin.locator('#user-search').fill('to_delete');
  await admin.locator('#user-list .job').filter({hasText:'@to_delete'}).waitFor();
  admin.once('dialog',dialog=>dialog.accept('nhap-sai'));
  await admin.locator('#user-list [data-user-action=delete]').click();
  await admin.locator('#notice.error').filter({hasText:'không khớp'}).waitFor();
  assert.equal(await admin.locator('#user-list .job').filter({hasText:'@to_delete'}).count(),1,'a wrong confirmation deletes nothing');
  admin.once('dialog',dialog=>dialog.accept('to_delete'));
  await admin.locator('#user-list [data-user-action=delete]').click();
  await admin.locator('#notice').filter({hasText:'Đã xóa tài khoản'}).waitFor();
  await admin.locator('#user-list .job').filter({hasText:'@to_delete'}).waitFor({state:'detached'});
  await doomed.reload();
  await doomed.locator('#auth-form').waitFor();
  assert.equal(await doomed.locator('#gate').isVisible(),true,'the deleted person is signed out');
  // Deleting a team: needs its exact name; the team leaves the list (hidden, since it entered a tournament).
  await admin.locator('#team-search').fill('Kéo Thần Tốc');
  await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).waitFor();
  admin.once('dialog',dialog=>dialog.accept('sai ten'));
  await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).locator('[data-team-action=delete]').click();
  await admin.locator('#notice.error').filter({hasText:'Tên đội không khớp'}).waitFor();
  assert.equal(await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).count(),1,'a wrong name deletes nothing');
  admin.once('dialog',dialog=>dialog.accept('Kéo Thần Tốc'));
  await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).locator('[data-team-action=delete]').click();
  await admin.locator('#notice').filter({hasText:'Đã xóa đội'}).waitFor();
  await admin.locator('#team-list .job').filter({hasText:'Kéo Thần Tốc'}).waitFor({state:'detached'});
  await admin.locator('#team-search').fill('');
  admin.on('dialog',dialog=>dialog.accept());
  await admin.locator('#cleanup-teams').click();
  await admin.locator('#notice').filter({hasText:/Đã dọn|Không có đội nào trống/}).waitFor();
  admin.removeAllListeners('dialog');
  await admin.setViewportSize({width:390,height:844});
  await noOverflow(admin);
  assert.deepEqual(errors,[]);
  console.log('PASS: registration, team create/join, bot validation, boss campaign clear + unlock, ranked queue/cancel, tournament entry, dashboard checklist/feed/badges, team profile, four replays with speed/expand, admin dashboard, tournament create, mobile layout.');
} finally {
  await browser?.close();
  for(const service of services) service.kill();
}
