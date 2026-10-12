const { chromium } = require('../.playwright/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const options = { headless: true };
  if (process.env.BROWSER_EXECUTABLE) options.executablePath = process.env.BROWSER_EXECUTABLE;
  else if (process.platform === 'win32') options.channel = 'msedge';
  const browser = await chromium.launch(options);
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(process.env.TEST_BASE_URL || 'http://127.0.0.1:8765/');
    await page.getByRole('link', { name: '멀티에이전트 수사 관제 →' }).click();
    await page.locator('#create').click();
    await page.locator('#failGM').click();
    await page.waitForFunction(() => document.querySelector('#controller').textContent.includes('gm-2'));
    assert.ok((await page.locator('#gmHistory').textContent()).includes('NPC'));
    for (const index of [0, 1]) {
      await page.locator('#agents .card').nth(index).getByRole('button').click();
      await page.waitForFunction(i => document.querySelectorAll('#agents .card')[i]?.textContent.includes('failed'), index);
    }
    await page.locator('#run').click();
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('사건 해결'), {}, { timeout: 20000 });
    assert.equal(await page.locator('#count').textContent(), '25');
    assert.equal(await page.locator('#tasks tr').count(), 8);
    await page.locator('#player').selectOption('agent-3');
    await page.locator('#inspect').click();
    await page.waitForFunction(() => document.querySelector('#private').textContent.includes('goal'));
    const download = page.waitForEvent('download');
    await page.locator('#export').click();
    await (await download).saveAs('.playwright/shared-export.json');
    const record = JSON.parse(fs.readFileSync('.playwright/shared-export.json', 'utf8'));
    assert.equal(record.won, true);
    assert.equal(record.shared.length, 25);
    assert.equal(record.metrics.active_agents, 1);
    assert.ok(record.metrics.reassignments >= 2);
    assert.ok(record.metrics.gm_failovers >= 1);
    assert.equal(record.gm_agents.filter(gm => gm.status === 'active').length, 1);
    for (const forbidden of ['truth', 'gm_token', 'self', 'agent_credentials', 'all_clues']) {
      assert.equal(record[forbidden], undefined);
    }
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('사건 해결'));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({ path: '.playwright/mobile.png', fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: navigation, create, GM succession/NPC/single leader, 3→1 failures, reassignment, solution, private observe, safe export, reload, mobile, no JS errors');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
