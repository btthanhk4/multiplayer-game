// Self-contained browser smoke. Uses disposable artifacts/ data and loopback only.
// PowerShell: $env:ARENA_PYTHON='.venv/Scripts/python.exe'; node scripts/arena-smoke.mjs
// Linux: ARENA_PYTHON=.venv/bin/python node scripts/arena-smoke.mjs
import {chromium} from 'playwright';
import {spawn,spawnSync} from 'node:child_process';
import {mkdirSync,mkdtempSync,readFileSync} from 'node:fs';
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
const noOverflow=async page=>{
  const wide=await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1?'':[...document.querySelectorAll('body *')].filter(el=>el.getBoundingClientRect().right>window.innerWidth+1).slice(0,4).map(el=>`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]}`).join(', '));
  assert.equal(wide,'','No horizontal overflow');
};
// Phone-width audit: no sideways scrolling and no text squeezed into a sliver, on every tab, at the narrowest real phones.
async function auditLayout(page,tabs,label){
  for(const width of [320,360,412]){
    await page.setViewportSize({width,height:800});
    for(const tab of tabs){
      await page.locator(`[role=tab][data-tab=${tab}]`).click();
      await page.waitForTimeout(200);
      const problems=await page.evaluate(()=>{
        const out=[],limit=window.innerWidth+2;
        if(document.documentElement.scrollWidth>window.innerWidth+1)out.push('page scrolls sideways');
        const scrolls=el=>{for(let p=el.parentElement;p&&p!==document.body;p=p.parentElement){const o=getComputedStyle(p).overflowX;if(o==='auto'||o==='scroll'||o==='hidden')return true}return false};
        for(const el of document.querySelectorAll('main *, .masthead *')){
          if(['OPTION','SCRIPT','STYLE','PATH','svg','SVG'].includes(el.tagName))continue;
          const box=el.getBoundingClientRect();
          if(!box.width||!box.height||getComputedStyle(el).visibility==='hidden')continue;
          const own=[...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim();
          const name=`${el.tagName.toLowerCase()}${el.className&&typeof el.className==='string'?'.'+el.className.split(' ')[0]:''}`;
          const font=parseFloat(getComputedStyle(el).fontSize)||12;
          if(el.classList.contains('sr-only'))continue;
          if(own.length>=8&&box.width<90&&box.height>font*4&&!scrolls(el))out.push(`squeezed ${name}: ${own.slice(0,24)} (${Math.round(box.width)}px)`);
          if(box.right>limit&&!scrolls(el)&&!el.closest('.table-scroll,pre,textarea'))out.push(`past the edge ${name}: ${own.slice(0,24)} (right ${Math.round(box.right)})`);
        }
        return [...new Set(out)].slice(0,8);
      });
      assert.deepEqual(problems,[],`${label} · ${tab} · ${width}px`);
    }
  }
}
// Colour audit: every visible piece of text keeps readable contrast against its real background in both themes.
async function auditContrast(page,tabs,label,shots){
  await page.emulateMedia({reducedMotion:'reduce'});   // pulsing elements change opacity mid-frame
  for(const theme of ['dark','light']){
    await page.evaluate(t=>document.documentElement.setAttribute('data-theme',t),theme);
    for(const tab of tabs){
      await page.locator(`[role=tab][data-tab=${tab}]`).click();
      await page.waitForTimeout(250);
      if(shots)await page.screenshot({path:`${shots}/${label}-${tab}-${theme}.png`,fullPage:true});
      const bad=await page.evaluate(()=>{
        const rgb=c=>{const m=c.match(/rgba?\(([^)]+)\)/);if(!m)return null;const v=m[1].split(/[ ,/]+/).filter(Boolean).map(Number);return {r:v[0],g:v[1],b:v[2],a:v[3]===undefined?1:v[3]}};
        const lum=({r,g,b})=>{const f=x=>{x/=255;return x<=.03928?x/12.92:((x+.055)/1.055)**2.4};return .2126*f(r)+.7152*f(g)+.0722*f(b)};
        const mix=(top,under)=>({r:top.r*top.a+under.r*(1-top.a),g:top.g*top.a+under.g*(1-top.a),b:top.b*top.a+under.b*(1-top.a),a:1});
        const backdrop=el=>{const layers=[];for(let n=el;n;n=n.parentElement){const st=getComputedStyle(n);let c=rgb(st.backgroundColor);
            if(st.backgroundImage&&st.backgroundImage!=='none'){const g=st.backgroundImage.match(/rgba?\([^)]+\)/);if(g)c=rgb(g[0])}
            if(c&&c.a>0){layers.push(c);if(c.a>=.99)break}}
          let base={r:255,g:255,b:255,a:1};for(const c of layers.reverse())base=mix(c,base);return base};
        const out=[];
        for(const el of document.querySelectorAll('main *, .masthead *, .gate *')){
          if(['OPTION','SCRIPT','STYLE','svg','path','TEXTAREA'].includes(el.tagName)||el.closest('pre,textarea,.editor-panel,.hero-art,.gate-art,.stone,.mini-board,.confetti,.locked,[disabled]'))continue;
          const own=[...el.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim();
          if(own.length<2||!/[\p{L}\p{N}]/u.test(own))continue;
          const box=el.getBoundingClientRect(),st=getComputedStyle(el);
          if(!box.width||!box.height||st.visibility==='hidden'||st.opacity==='0'||el.closest('[hidden]'))continue;
          const fg=rgb(st.color),bg=backdrop(el);
          const text=mix({...fg,a:fg.a*(parseFloat(st.opacity)||1)},bg);
          const [hi,lo]=[lum(text),lum(bg)].sort((x,y)=>y-x);
          const ratio=(hi+.05)/(lo+.05);
          const large=parseFloat(st.fontSize)>=24||(parseFloat(st.fontSize)>=18.6&&Number(st.fontWeight)>=700);
          if(ratio<(large?3:4.3))out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ')[0]} "${own.slice(0,22)}" ${ratio.toFixed(2)}`);
        }
        return [...new Set(out)].slice(0,10);
      });
      assert.deepEqual(bad,[],`${label} · ${tab} · ${theme} contrast`);
    }
  }
  await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
  await page.emulateMedia({reducedMotion:'no-preference'});
}
// Starts a practice pair, retrying while the team's previous pair is still queued or running (the server allows one at a time).
async function practice(page){
  for(let attempt=0;attempt<40;attempt++){
    const response=page.waitForResponse(r=>r.url().includes('/api/practice'));
    await page.locator('#practice-form button[type=submit]').click();
    if((await response).status()!==409) return;
    await page.waitForTimeout(1500);
  }
  throw new Error('practice pair never started');
}
async function newPage(width=1440){
  const context=await browser.newContext({viewport:{width,height:1000}});
  const page=await context.newPage();
  page.on('pageerror',e=>{errors.push(e.message);console.error('PAGEERROR',e.message)});
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
  // Obstacle and 8×8 maps: the warning shows, and replays draw the right board.
  await page.locator('[role=tab][data-tab=lab]').click();
  await page.locator('#practice-map').selectOption('river');
  await page.locator('#practice-map-note').waitFor();
  await practice(page);
  await page.locator('#watch-grid .replay-caption').filter({hasText:'Bản đồ Dòng sông'}).first().waitFor({timeout:60000});
  assert.equal(await page.locator('#watch-grid .mini-board .square.wall').count() % 7,0,'the river map shows its seven obstacle squares per board');
  assert.ok(await page.locator('#watch-grid .mini-board .square.wall').count() >= 7);
  while (await page.locator('#watch-grid .close-button').count()) await page.locator('#watch-grid .close-button').first().click();
  await page.locator('[role=tab][data-tab=lab]').click();
  await page.locator('#practice-map').selectOption('arena8');
  await practice(page);
  await page.locator('#watch-grid .replay-caption').filter({hasText:'Bản đồ Đấu trường 8×8'}).first().waitFor({timeout:60000});
  assert.equal(await page.locator('#watch-grid .mini-board.n8').first().locator('.square').count(),64);
  await page.locator('[role=tab][data-tab=lab]').click();
  await page.locator('#practice-map').selectOption('standard');
  await page.locator('[role=tab][data-tab=competition]').click();
  await page.locator('#join-form button[type=submit]').click();
  await page.locator('#competition-info summary').filter({hasText:'Luật chơi của giải này'}).click();
  await page.locator('#competition-info .round-rules').getByText('Vòng 1').first().waitFor();
  await page.locator('#competition-info .round-rules').getByText('Thông tin bản đồ để viết bot').waitFor();
  assert.ok(await page.locator('#competition-info .map-card .small-table').first().innerText().then(x=>/Ô đích/.test(x)&&/Quân Xanh/.test(x)));
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
  // Stepping one ply forward slides the moved piece; pieces are drawn as icons, not letters.
  await page.locator('[data-step="1"]').click();
  assert.equal(await page.locator('.mini-board .stone.slide').count(),1,'one ply forward animates exactly one piece');
  assert.ok(await page.locator('.mini-board .stone .glyph').count()>=6);
  await page.locator('[data-step="-1"]').click();
  assert.equal(await page.locator('.mini-board .stone.slide').count(),0,'stepping back does not animate');
  // The board picker restyles every board at once and is remembered after a reload.
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.skin),'wood');
  await page.locator('.masthead .skin-picker select').selectOption('marble');
  assert.equal(await page.evaluate(()=>document.documentElement.dataset.skin),'marble');
  assert.equal(await page.locator('.watch-skin select').inputValue(),'marble','every picker on the page follows the choice');
  assert.equal(await page.evaluate(()=>localStorage.getItem('ottv2-skin')),'marble','the choice is saved for the next visit');
  await page.locator('.masthead .skin-picker select').selectOption('wood');
  await page.evaluate(()=>document.activeElement.blur());
  await page.keyboard.press('Home');
  assert.match(await page.locator('.watch-note').textContent(),/Nước 0\//);
  await page.keyboard.press('End');
  assert.match(await page.locator('.watch-note').textContent(),new RegExp(`Nước ${compact.length}/`));
  if(real.sibling) await page.locator('[data-sibling]').waitFor();
  await page.screenshot({path:'artifacts/arena-watch-live.png',fullPage:true});
  // A finished game saves as a file; a file from local_arena.py --replay opens in the browser without the server.
  await page.evaluate(()=>document.querySelectorAll('.moves-box').forEach(box=>{box.open=true;}));
  const [download]=await Promise.all([page.waitForEvent('download'),page.locator('[data-download]').click()]);
  const saved=JSON.parse(readFileSync(await download.path(),'utf8'));
  assert.equal(saved.format,'ottv2-replay');
  assert.equal(saved.moves.length,compact.length,'the saved replay holds every move');
  await page.locator('#replay-file').setInputFiles({name:'van-1.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(saved))});
  const local=page.locator('.watch-card').filter({hasText:'TỪ MÁY BẠN'});
  await local.waitFor();
  assert.equal(await local.locator('[data-r=count]').textContent(),String(compact.length),'every move of the file is listed');
  assert.equal(await local.locator('[data-share]').count(),0,'a file from this computer has no share link');
  await local.locator(`[data-r=moves] button[data-ply="${compact.length}"]`).click();
  await local.locator('.result-banner').waitFor();
  await local.locator('.close-button').click();
  await page.locator('#replay-file').setInputFiles({name:'hong.json',mimeType:'application/json',buffer:Buffer.from('{"moves":[{"from":[0,0],"to":[5,5]}]}')});
  await page.locator('#notice.error').filter({hasText:'không khớp luật'}).waitFor();
  await page.locator('#replay-file').setInputFiles({name:'rac.json',mimeType:'application/json',buffer:Buffer.from('not json')});
  await page.locator('#notice.error').filter({hasText:'không phải JSON'}).waitFor();
  assert.equal(await page.locator('.watch-card').count(),1,'refused files open no board');
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

  await auditLayout(page,['overview','team','lab','campaign','ranked','competition','watch'],'student');
  await page.setViewportSize({width:1440,height:1000});
  await auditContrast(page,['overview','team','lab','campaign','ranked','competition','watch'],'student',process.env.ARENA_SHOTS);
  await page.setViewportSize({width:1440,height:1000});

  // A newcomer who plays alone: one click makes the team, the no-code builder writes the bot, the lab explains the result.
  const solo=await newPage();
  await register(solo,'solo_walker','Người đi một mình');
  await solo.locator('[role=tab][data-tab=overview]').click();
  await solo.locator('#next-step [data-solo]').waitFor();                      // the dashboard offers the same shortcut
  await solo.locator('[role=tab][data-tab=team]').click();
  await solo.locator('#solo-team').click();
  await solo.locator('#lab:not([hidden])').waitFor();
  await solo.locator('#practice-a').waitFor({state:'attached'});
  const original=await solo.locator('#source').inputValue();
  assert.equal(await solo.locator('#undo-load').isHidden(),true);
  await solo.locator('[data-preset=attack]').click();
  assert.equal(await solo.locator('#slider-CAPTURE').inputValue(),'25');
  await solo.locator('#slider-SAFETY').fill('7');
  assert.equal(await solo.locator('#builder-presets [aria-pressed=true]').count(),0,'moving a slider leaves the preset');
  await solo.locator('#builder-use').click();
  await solo.waitForFunction(()=>document.querySelector('#source').value.includes('CAPTURE = 25'));
  assert.match(await solo.locator('#source').inputValue(),/SAFETY = 7\b/);
  assert.equal(await solo.locator('#bot-name').inputValue(),'Bot tùy chỉnh');
  await solo.locator('#undo-load').waitFor();
  await solo.locator('#undo-load').click();
  assert.equal(await solo.locator('#source').inputValue(),original,'undo brings back what was in the editor');
  assert.equal(await solo.locator('#undo-load').isHidden(),true);
  // The smoke worker only runs unmodified examples, and the "balanced" preset is exactly sdk/examples/custom.py.
  await solo.locator('[data-preset=balanced]').click();
  await solo.locator('#builder-use').click();
  await solo.waitForFunction(()=>document.querySelector('#bot-name').value==='Bot Cân bằng');
  await solo.locator('#source').focus();
  await solo.keyboard.press('Control+Enter');
  await solo.locator('#lab-status.good').waitFor({timeout:90000});
  await solo.locator('#lab-status [data-boss-bot]').waitFor();
  await solo.screenshot({path:'artifacts/arena-lab-valid.png',fullPage:true});
  // A rejected bot is explained in plain Vietnamese (the server answer is stubbed: the smoke worker cannot reject code).
  await solo.route('**/api/submissions',async route=>{
    const real=await route.fetch(), data=await real.json();
    data.items[0]={...data.items[0],status:'rejected',log:'Không nạp được bot: SyntaxError: invalid syntax (<string>, line 2)'};
    await route.fulfill({response:real,json:data});
  });
  await solo.locator('[role=tab][data-tab=overview]').click();
  await solo.locator('[role=tab][data-tab=lab]').click();
  await solo.locator('#lab-status.bad').filter({hasText:'Lỗi cú pháp Python'}).waitFor({timeout:60000});
  await solo.locator('#lab-status').filter({hasText:'dòng 2'}).waitFor();
  await solo.locator('#lab-status details summary').click();
  await solo.locator('#lab-status .log-text').filter({hasText:'SyntaxError'}).waitFor();
  await auditLayout(solo,['lab','team','overview'],'solo');
  await solo.setViewportSize({width:390,height:900});
  await solo.locator('[role=tab][data-tab=lab]').click();
  await solo.screenshot({path:'artifacts/arena-lab-mobile.png',fullPage:true});
  await solo.setViewportSize({width:1440,height:1000});
  await auditContrast(solo,['lab'],'solo',process.env.ARENA_SHOTS);
  await solo.locator('[role=tab][data-tab=lab]').click();
  await solo.locator('#lab-status [data-dismiss-lab]').click();
  await solo.locator('#lab-status').waitFor({state:'hidden'});
  await solo.reload();
  await solo.locator('#lab:not([hidden])').waitFor({state:'attached'});
  await solo.locator('[role=tab][data-tab=lab]').click();
  assert.equal(await solo.locator('#lab-status').isHidden(),true,'a dismissed notice stays dismissed after a reload');

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
  await admin.locator('#next-action').filter({hasText:'Đang nhận đăng ký'}).waitFor();
  await admin.locator('#edit-details summary').click();
  await admin.locator('#tournament-edit [name=map]').selectOption('block');
  await admin.locator('#tournament-edit [name=name]').fill('Giải đã đổi tên');
  await admin.locator('#tournament-edit button.primary').click();
  await admin.locator('#admin-tournament option').filter({hasText:'Giải đã đổi tên'}).waitFor({state:'attached'});
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#close-now').click();
  await admin.locator('#freeze-button:not([disabled])').waitFor({state:'attached',timeout:15000});
  await admin.locator('#add-entry-details summary').click();
  await admin.locator('#add-entry [name=team]').fill('Không có đội này');
  await admin.locator('#add-entry button').click();
  await admin.locator('#notice.error').waitFor();
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#cancel-tournament').click();
  await admin.locator('#admin-tournament option').filter({hasText:'Đã hủy'}).waitFor({state:'attached'});
  await admin.locator('#cancel-tournament:disabled').waitFor({timeout:15000});
  // A private tournament: the organiser sees the code, a team must type it, then waits in the waiting room.
  await admin.locator('#tournament-form [name=name]').fill('Giải mã riêng');
  await admin.locator('#tournament-form [name=private]').check();
  await admin.locator('#tournament-form button.primary').click();
  await admin.locator('#code-box:not([hidden]) #join-code-value').filter({hasText:/^[A-Z2-9]{6}$/}).waitFor();
  const joinCode=(await admin.locator('#join-code-value').textContent()).trim();
  assert.equal(await admin.locator('#start-button').isDisabled(),false);
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#start-button').click();
  await admin.locator('#notice.error').filter({hasText:'ít nhất hai bot'}).waitFor();     // one team is not a tournament; nothing changes
  await admin.locator('#tournament-state').filter({hasText:'Đang nhận đăng ký'}).waitFor();
  await page.locator('[role=tab][data-tab=competition]').click();
  await page.locator('#competition-select option').filter({hasText:'🔒 Giải mã riêng'}).waitFor({state:'attached',timeout:15000});
  await page.locator('#competition-select').selectOption({label:'🔒 Giải mã riêng'});
  await page.locator('#join-code-field:not([hidden])').waitFor();
  await page.locator('#join-code').fill('ZZZZZZ');
  await page.locator('#join-form button.primary').click();
  await page.locator('#notice.error').filter({hasText:'Mã tham gia không đúng'}).waitFor();
  assert.equal(await page.locator('#waiting-room').isHidden(),true);
  await page.locator('#join-code').fill(joinCode.toLowerCase());
  await page.locator('#join-form button.primary').click();
  await page.locator('#waiting-room:not([hidden])').filter({hasText:'chờ ban tổ chức bắt đầu'}).waitFor();
  await page.locator('#join-code-field').waitFor({state:'hidden'});
  await page.screenshot({path:'artifacts/arena-waiting-room.png',fullPage:true});
  // The organiser starts the fixture tournament; every team follows it from the dashboard and the tournament tab.
  await admin.locator('#admin-tournament').selectOption({label:'Khai màn · Mùa chiến thuật · Đang nhận đăng ký'});
  await admin.locator('#tournament-state').filter({hasText:'4 đăng ký'}).waitFor();
  await admin.locator('#start-button:not([disabled])').waitFor();
  admin.once('dialog',dialog=>dialog.accept());
  await admin.locator('#start-button').click();
  await admin.locator('#notice').filter({hasText:'Giải đã bắt đầu'}).waitFor();
  await page.locator('[role=tab][data-tab=overview]').click();
  await page.locator('#open-tournament .banner.live').filter({hasText:'GIẢI ĐANG DIỄN RA'}).waitFor({timeout:60000});
  await page.locator('#open-tournament .banner.live [data-tournament]').click();
  await page.locator('#my-journey:not([hidden])').filter({hasText:'Vòng 1'}).waitFor({timeout:60000});
  await page.locator('#my-journey .journey-list li').filter({hasText:/Thắng|Thua|Hòa|BYE|nghỉ/}).first().waitFor({timeout:120000});
  assert.ok(await page.locator('#my-journey [data-watch]').count()>=1 || await page.locator('#my-journey').getByText('nghỉ').count()>=1);
  await page.screenshot({path:'artifacts/arena-journey.png',fullPage:true});
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
  const guide=await newPage(320);
  for(const width of [320,360,412]){
    await guide.setViewportSize({width,height:800});
    await guide.goto(base+'/arena/guide.html');
    await guide.locator('#map-river').waitFor();
    await noOverflow(guide);
  }
  await guide.close();
  await auditLayout(admin,['dashboard','events','people','bots','matches','queue','log'],'admin');
  await auditContrast(admin,['dashboard','events','people','bots','matches','queue','log'],'admin',process.env.ARENA_SHOTS);
  assert.deepEqual(errors,[]);
  console.log('PASS: registration, team create/join, bot validation, boss campaign clear + unlock, ranked queue/cancel, tournament entry, dashboard checklist/feed/badges, team profile, four replays with speed/expand, admin dashboard, tournament create, mobile layout.');
} finally {
  await browser?.close();
  for(const service of services) service.kill();
}
