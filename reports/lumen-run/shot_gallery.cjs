// Screenshot the calm-ds component gallery for the company report.
const { chromium } = require('@playwright/test');
const fs = require('fs');

(async () => {
  const dir = 'q:\\projects\\inspired-arc\\podium\\reports\\lumen-run\\screenshots';
  fs.mkdirSync(dir, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1000, height: 760 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto('http://127.0.0.1:4192/gallery.html', { waitUntil: 'networkidle', timeout: 60000 });
  try {
    await page.waitForFunction(() => window.__GALLERY_READY__ === true, { timeout: 45000 });
    await page.waitForTimeout(1200);
  } catch (e) {
    console.log('gallery ready wait failed:', e.message);
  }
  console.log('console errors:', JSON.stringify(errors.slice(0, 5)));
  await page.screenshot({ path: dir + '\\2-design-system-components.png', fullPage: true });
  await browser.close();
  console.log('saved 2-design-system-components.png');
})();
