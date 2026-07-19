// Screenshot the built notes app for the company report.
const { chromium } = require('@playwright/test');
const fs = require('fs');

(async () => {
  const dir = 'q:\\projects\\inspired-arc\\podium\\reports\\lumen-run\\screenshots';
  fs.mkdirSync(dir, { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1200, height: 780 } });
  await page.goto('http://127.0.0.1:4190/', { waitUntil: 'networkidle' });
  const md = [
    '# Lumen — Calm Notes',
    '',
    'A **distraction-free** markdown notes app.',
    '',
    '## Today',
    '- [x] Ship sprint 1',
    '- Write the *brand & voice* guide',
    '- Review the calm design system',
    '',
    '## Why Lumen',
    '> Clarity that moves you forward.',
    '',
    'Links stay safe — rendering is **sanitized**:',
    '[lumen.app](https://lumen.app)',
    '',
    '```js',
    'const focus = () => save(note);',
    '```',
    ''
  ].join('\n');
  const ta = page.getByRole('textbox', { name: 'Markdown editor' });
  await ta.fill(md);
  await page.waitForTimeout(500);
  await page.screenshot({ path: dir + '\\1-markdown-notes-app.png' });
  await browser.close();
  console.log('saved 1-markdown-notes-app.png');
})();
