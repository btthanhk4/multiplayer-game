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
                spawn(python,['-m','arena_backend','--data',data,'worker','--mode','samples'],{env,stdio:'pipe'})];
let browser;
const errors=[];
try {
  for (let i=0;i<80;i++) {try{if((await fetch(base+'/api/health')).ok)break;}catch{} await new Promise(r=>setTimeout(r,250));}
  browser=await chromium.launch(process.platform==='win32'?{executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true}:{headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error' && /Content Security Policy|Refused to/.test(m.text()))errors.push(m.text());});
  await page.goto(base+'/arena/');
  await page.getByRole('heading',{name:'Khai màn · Mùa chiến thuật'}).waitFor();
  await page.screenshot({path:'artifacts/arena-desktop.png',fullPage:true});
  await page.locator('#account-button').click(); await page.locator('#toggle-auth').click();
  await page.locator('#auth-form [name=username]').fill('browser_player');
  await page.locator('#auth-form [name=password]').fill(password);
  await page.locator('#auth-form [name=name]').fill('Người viết chiến thuật');
  await page.locator('#auth-submit').click(); await page.locator('#auth-dialog').waitFor({state:'hidden'});
  await page.getByRole('tab',{name:/Xưởng bot/}).click();
  await page.locator('[data-sample=hunter]').click();
  await page.locator('#submit-form button[type=submit]').click();
  await page.locator('.submission .badge').filter({hasText:'Hợp lệ'}).waitFor({timeout:60000});
  await page.screenshot({path:'artifacts/arena-lab.png',fullPage:true});
  await page.getByRole('tab',{name:/Bảng điểm/}).click();
  await page.locator('#join-form button').click();
  await page.locator('#entry-status').filter({hasText:'Bạn đã chọn'}).waitFor();
  // Player cannot obtain admin endpoints or other users' source.
  assert.equal((await page.request.get(base+'/api/admin/jobs')).status(),403);
  await page.getByRole('tab',{name:/Khán đài/}).click();
  for(let i=0;i<4;i++) await page.locator('#match-list [data-watch]').nth(i).click();
  await page.locator('#watch-grid .mini-board').nth(3).waitFor();
  await page.locator('#match-list [data-watch]').nth(4).click();
  assert.equal(await page.locator('#watch-grid .watch-card').count(),4);
  assert.match(await page.locator('#notice').textContent(),/bốn bàn/);
  const slider=page.locator('[data-replay]').first();
  await slider.fill('0');await slider.dispatchEvent('change');
  assert.match(await page.locator('.watch-note').first().textContent(),/Nước 0/);
  const dimensions=await page.locator('.mini-board').first().evaluate(el=>[...el.children].map(c=>({w:c.getBoundingClientRect().width,h:c.getBoundingClientRect().height})));
  assert.ok(dimensions.every(d=>Math.abs(d.w-d.h)<1));
  await page.screenshot({path:'artifacts/arena-watch.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'No horizontal overflow');
  await page.evaluate(()=>window.scrollTo(0,document.body.scrollHeight));
  assert.ok(await page.evaluate(()=>window.scrollY>0),'Four boards scroll vertically');
  await page.screenshot({path:'artifacts/arena-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#account-button').click(); await page.locator('#account-button').click();
  await page.locator('#toggle-auth').click();
  await page.locator('#auth-form [name=username]').fill('arena_admin');
  await page.locator('#auth-form [name=password]').fill(password);
  await page.locator('#auth-submit').click(); await page.locator('#auth-dialog').waitFor({state:'hidden'});
  await page.getByRole('tab',{name:/Điều hành/}).click();
  await page.locator('#tournament-form [name=name]').fill('Giải trình duyệt');
  await page.locator('#tournament-form button').click();
  await page.locator('#admin-tournament').getByRole('option',{name:'Giải trình duyệt'}).waitFor({state:'attached'});
  assert.equal(await page.locator('#freeze-button').isDisabled(),true);
  await page.screenshot({path:'artifacts/arena-admin.png',fullPage:true});
  assert.deepEqual(errors,[]);
  console.log('PASS: registration, upload, Python sample validation, tournament entry, read-only access, four replays, square cells, mobile scrolling, admin create.');
} finally {
  await browser?.close();
  for(const service of services) service.kill();
}
