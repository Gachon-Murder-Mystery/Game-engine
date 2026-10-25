const {chromium}=require('../.playwright/node_modules/playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch(process.env.BROWSER_EXECUTABLE?{headless:true,executablePath:process.env.BROWSER_EXECUTABLE}:{headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.TEST_BASE_URL+'/competition');
  await page.locator('#role').selectOption('operator');await page.locator('#game').fill('case');
  await page.locator('#token').fill(process.env.TEST_OPERATOR_TOKEN);await page.locator('#connect').click();
  await page.waitForFunction(()=>document.querySelector('#positions').children.length===12);
  assert.equal(await page.locator('#graph line').count(),11);
  await page.locator('#owner').selectOption('1');assert.equal(await page.locator('#graph circle').count(),12);
  page.on('dialog',d=>d.accept());await page.locator('[data-operation=PAUSE]').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('paused'));
  await page.locator('[data-operation=RESUME]').click();
  await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('running'));
  await page.locator('#role').selectOption('observer');await page.locator('#token').fill(process.env.TEST_OBSERVER_TOKEN);
  await page.locator('#connect').click();await page.waitForFunction(()=>document.querySelector('#relationships').hidden);
  assert.equal(await page.locator('#operator-controls').isVisible(),false);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
  await page.screenshot({path:'.playwright/competition-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log('PASS: competitive operator/spectator, 12 characters, directed graph, pause/resume, privacy, mobile, no JS errors');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
