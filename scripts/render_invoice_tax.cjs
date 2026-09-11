// Local fictional artifacts only. All HTTP(S) requests are denied.
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require(path.join(process.env.USERPROFILE, '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));

(async () => {
  const root = path.resolve(__dirname, '..');
  const dir = path.join(root, 'docs/verification-artifacts');
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 960 } });
    await context.route(/^https?:/, route => route.abort());
    const page = await context.newPage();
    const results = [];
    for (const name of ['tax-standard', 'tax-mixed', 'tax-legacy']) {
      await page.goto(pathToFileURL(path.join(dir, name + '-print.html')).href);
      await page.pdf({ path: path.join(dir, name + '.pdf'), printBackground: true, preferCSSPageSize: true });
      results.push({ name, pdfCreated: true });
    }
    await page.goto(pathToFileURL(path.join(dir, 'tax-step4-form.html')).href);
    const tax = page.locator('[data-tax-category]').first();
    await tax.selectOption('10');
    await page.locator('[data-client-amount]').first().fill('1000');
    const confirmed = await page.locator('#invoice-tax-preview').innerText();
    const total = await page.locator('#client-total').innerText();
    if (!confirmed.includes('90') || !total.includes('1,000')) throw new Error('Standard preview does not preserve gross/tax');
    await tax.selectOption('unknown');
    const unknown = await page.locator('#invoice-tax-preview').innerText();
    if (!unknown.includes('税区分未確認 1点')) throw new Error('Unknown tax category is not explicit');
    results.push({ name: 'tax-selector-browser', confirmed, total, unknown, pass: true });
    fs.writeFileSync(path.join(root, 'docs/invoice-tax-browser-verification.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify(results));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
