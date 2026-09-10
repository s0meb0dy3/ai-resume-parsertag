// npm test：先 npx playwright install chromium，或设置 PLAYWRIGHT_CHROMIUM_EXECUTABLE。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');

test('三模块导航、草稿隔离、多选、测试、确认及移动抽屉', async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {}),
    args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    let current = {syncModel: 'Qwen-A', asyncModel: 'Qwen-B', fallbackModels: ['Qwen-C'], version: 2, source: 'runtime', updatedAt: ''};
    let previous = {...current, syncModel: 'Previous'};
    let writes = 0, catalogFail = false, rejectSave = false, probeFail = false;
    let catalog = ['Qwen-A', 'Qwen-B', 'Qwen-C', 'DeepSeek-X', 'New-model'];
    await page.route('http://admin.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/api/')) {
        if (url.pathname.endsWith('/catalog')) return route.fulfill({status: catalogFail ? 502 : 200, json: catalogFail ? {detail: '网关暂不可用'} : catalog});
        if (url.pathname.endsWith('/test')) {
          const payload = route.request().postDataJSON();
          return route.fulfill({json: {kind: payload.kind, results: [...new Set([payload.syncModel, payload.asyncModel, ...payload.fallbackModels])].map(model => ({model, passed: !probeFail, seconds: .1, message: probeFail ? '模拟模型错误详情' : '通过'}))}});
        }
        if (route.request().method() === 'PUT') {
          if (rejectSave) return route.fulfill({status: 409, json: {detail: '配置已更新，请刷新后重试'}});
          const body = route.request().postDataJSON(); assert.equal(body.expectedVersion, current.version); writes++;
          previous = {...current}; current = {...current, ...body, version: current.version + 1};
        }
        if (url.pathname.endsWith('/rollback')) { writes++; current = {...previous, version: current.version + 1}; }
        return route.fulfill({json: {current, previous}});
      }
      const filename = url.pathname.endsWith('.css') ? 'assets/model-config.css' : url.pathname.endsWith('.js') ? 'assets/model-config.js' : 'model-config.html';
      return route.fulfill({body: fs.readFileSync(path.join(__dirname, '..', filename)), contentType: filename.endsWith('.css') ? 'text/css' : filename.endsWith('.js') ? 'text/javascript' : 'text/html'});
    });
    await page.goto('http://admin.test/admin/models');
    await page.fill('#token', 'local-test'); await page.click('#connect');
    await page.waitForFunction(() => document.querySelector('#catalogStatus').textContent.includes('已加载'));
    assert(await page.locator('#view-overview').isVisible());
    const beforeToast = await page.locator('#view-overview').boundingBox();
    await page.click('#refresh');
    await page.waitForFunction(() => !document.querySelector('#notice').hidden);
    assert.equal(await page.locator('#notice').evaluate(el => getComputedStyle(el).position), 'fixed');
    assert.deepEqual(await page.locator('#view-overview').boundingBox(), beforeToast);
    await page.waitForFunction(() => document.querySelector('#notice').hidden, null, {timeout: 4000});
    assert.equal(await page.locator('#sidebar').evaluate(el => getComputedStyle(el).position), 'fixed');
    await page.locator('nav [data-nav="config"]').click();
    assert.equal(await page.locator('#modelList [data-model]').count(), 5);
    assert.equal(await page.locator('#modelGraph').count(), 0);
    await page.fill('#modelSearch', 'Qwen');
    const expandedHeight = (await page.locator('#modelExplorer').boundingBox()).height;
    await page.click('#explorerToggle');
    assert.equal(await page.getAttribute('#explorerToggle', 'aria-expanded'), 'false');
    assert(!(await page.locator('#explorerBody').isVisible()));
    assert((await page.locator('#modelExplorer').boundingBox()).height < expandedHeight);
    await page.click('#explorerToggle');
    assert.equal(await page.inputValue('#modelSearch'), 'Qwen');
    await page.fill('#modelSearch', '');
    assert(!(await page.textContent('#sidebar')).includes('安全热更新'));
    assert(!(await page.textContent('#view-overview')).includes('运行时配置'));
    assert.equal(await page.locator('#modelDetail').count(), 0);
    for (const viewport of [{width: 1366, height: 768}, {width: 1920, height: 1080}]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight + 1), false);
      const applyBox = await page.locator('#apply').boundingBox();
      assert(applyBox.y + applyBox.height <= viewport.height);
    }
    await page.click('#sidebarToggle');
    await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().width === 80);
    assert.equal(await page.getAttribute('#sidebarToggle', 'aria-expanded'), 'false');
    await page.getByRole('button', {name: '模型测试', exact: true}).click();
    await page.getByRole('button', {name: '模型配置', exact: true}).click();
    assert.equal(await page.locator('#workspace').evaluate(el => getComputedStyle(el).paddingLeft), '80px');
    await page.click('#sidebarToggle');
    await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().width === 240);
    await page.fill('#modelSearch', 'DeepSeek'); assert.equal(await page.locator('#modelList [data-model]').count(), 1); assert.equal(await page.locator('#sync option').count(), 2);
    await page.selectOption('#sync', 'DeepSeek-X'); await page.fill('#modelSearch', '');
    await page.click('#fallbackToggle');
    await page.locator('#fallbackOptions label').filter({hasText: 'Qwen-A'}).locator('input').check();
    await page.locator('#fallbackOptions label').filter({hasText: 'Qwen-B'}).locator('input').check();
    assert.equal(await page.locator('#fallbackOrder li').count(), 3);
    await page.click('#fallbackOrderSummary');
    await page.locator('#fallbackOrder li').nth(2).getByRole('button', {name: '上移 Qwen-B', exact: true}).click();
    assert.equal(await page.locator('#fallbackOrder .model-name').nth(1).textContent(), 'Qwen-B');
    await page.click('#fallbackDone');
    catalogFail = true; await page.click('#catalog'); await page.waitForFunction(() => document.querySelector('#catalogStatus').textContent.includes('加载失败'));
    catalogFail = false; await page.click('#catalog'); await page.waitForFunction(() => document.querySelector('#catalogStatus').textContent.includes('已加载'));
    await page.locator('nav [data-nav="overview"]').click(); assert.equal(await page.textContent('#currentSync'), 'Qwen-A');
    await page.locator('nav [data-nav="config"]').click(); assert.equal(await page.inputValue('#sync'), 'DeepSeek-X');
    assert(await page.locator('#draftDot').isVisible());
    await page.click('#testDraft'); await page.click('#textTest');
    await page.waitForFunction(() => document.querySelector('#testRows').textContent.includes('通过'));
    await page.locator('[data-target="current"]').click(); assert(!(await page.textContent('#testRows')).includes('通过'));
    await page.locator('[data-target="draft"]').click(); assert((await page.textContent('#testRows')).includes('通过'));
    await page.locator('nav [data-nav="config"]').click(); await page.selectOption('#sync', 'New-model');
    await page.click('#testDraft'); assert(!(await page.textContent('#testRows')).includes('通过'));
    await page.locator('nav [data-nav="config"]').click(); await page.click('#apply'); assert(await page.locator('#confirmDialog').isVisible());
    assert(await page.locator('#dialogWarning').isVisible()); await page.keyboard.press('Escape'); assert.equal(writes, 0);
    rejectSave = true; await page.click('#apply'); await page.click('#dialogConfirm');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('配置已更新'));
    assert.equal(await page.inputValue('#sync'), 'New-model');
    rejectSave = false; await page.click('#apply'); await page.click('#dialogConfirm');
    await page.waitForFunction(() => document.querySelector('#currentSync').textContent === 'New-model'); assert.equal(writes, 1);
    await page.locator('nav [data-nav="config"]').click();
    await page.click('#rollback'); await page.click('#dialogConfirm'); await page.waitForFunction(() => document.querySelector('#currentSync').textContent === 'Qwen-A'); assert.equal(writes, 2);
    await page.locator('nav [data-nav="config"]').click();
    catalog = [...new Set([...catalog, ...Array.from({length: 100}, (_, i) => 'model-' + i + '-long-name-for-viewing')])];
    await page.click('#catalog'); await page.waitForFunction(() => document.querySelectorAll('#modelList [data-model]').length === 105);
    await page.fill('#modelSearch', 'no-model-matches'); assert.equal(await page.locator('#modelList [data-model]').count(), 0);
    await page.fill('#modelSearch', ''); assert.equal(await page.locator('#modelList [data-model]').count(), 105);
    await page.click('#sidebarToggle');
    // 最大主备数量的结果只在表格内部滚动，失败说明按需展开。
    current = {...current, fallbackModels: Array.from({length: 8}, (_, i) => 'backup-' + i)};
    await page.locator('nav [data-nav="overview"]').click(); await page.click('#refresh');
    await page.getByRole('button', {name: '测试当前模型', exact: true}).click();
    probeFail = true; await page.click('#visionTest');
    await page.waitForFunction(() => document.querySelector('#testRows').textContent.includes('失败原因'));
    assert.equal(await page.locator('#testRows tr').count(), 10);
    for (const viewport of [{width: 1366, height: 768}, {width: 1920, height: 1080}]) {
      await page.setViewportSize(viewport);
      assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight + 1), false);
    }
    await page.locator('#testRows summary').first().click();
    assert(await page.getByText('模拟模型错误详情', {exact: true}).first().isVisible());
    await page.setViewportSize({width: 390, height: 844});
    await page.click('#menu'); assert.equal(await page.getAttribute('#menu', 'aria-expanded'), 'true');
    await page.locator('nav [data-nav="config"]').click(); assert.equal(await page.getAttribute('#menu', 'aria-expanded'), 'false');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    await page.click('#menu'); await page.click('#logout'); await page.waitForSelector('#login:not([hidden])');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
