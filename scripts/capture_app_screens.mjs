// Capture screenshots of a running Vite app with a standalone headless Chromium.
//
// Usage: node capture_app_screens.mjs <appDir> <url> <outDir>
//   <appDir> — the app repo dir that has node_modules/@playwright/test (browser resolution root)
//   <url>    — the dev/preview server URL (e.g. http://127.0.0.1:5173/)
//   <outDir> — where the PNGs are written (the report's screenshots/ dir)
//
// Reusable across runs: it resolves Playwright's chromium from the APP's node_modules (createRequire),
// so it works for any company's merged repo without installing Playwright into podium itself.
import { createRequire } from "module";
import { pathToFileURL } from "url";

const [, , appDir, url, outDir] = process.argv;
if (!appDir || !url || !outDir) {
  console.error("usage: node capture_app_screens.mjs <appDir> <url> <outDir>");
  process.exit(2);
}

const require = createRequire(pathToFileURL(appDir.replace(/\\/g, "/") + "/"));
const { chromium } = require("@playwright/test");
const fs = require("fs");
fs.mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 });
await page.goto(url, { waitUntil: "load" });
await page.waitForTimeout(700);

// 01 — the app as it loads: split editor + live preview
await page.evaluate(() => document.activeElement && document.activeElement.blur());
await page.screenshot({ path: `${outDir}/01-split-editor-live-preview.png` });

// 02 — type real markdown and show the live preview render
try {
  const ta = page.locator("textarea").first();
  await ta.click();
  await ta.fill(
    "# Calm Notes\n\nA **distraction-free** markdown editor for calm users.\n\n" +
      "## Why it's calm\n- Live preview as you type\n- Autosaves to your browser\n- Export & import `.md`\n\n" +
      "> Write without clutter.\n\nInline `code`, a [link](https://example.com), and a list:\n\n" +
      "1. Open a note\n2. Write\n3. It saves itself\n"
  );
  await page.waitForTimeout(500);
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  await page.screenshot({ path: `${outDir}/02-live-preview-rendered.png` });
} catch (e) {
  console.error("02 capture skipped:", e.message);
}

await browser.close();
console.log("captured screenshots to", outDir);
