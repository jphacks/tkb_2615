/* Run against an already-running SHELTERSIM_LLM_MODE=offline server only.
 * PLAYWRIGHT_MODULE=/absolute/path/to/playwright node scripts/test_demo_v2.cjs
 * Optional: UI_BASE_URL, UI_SCREENSHOT_DIR, CHROME_CHANNEL (default chrome).
 * Creates a fresh temporary Project through real HTTP APIs. No request mocks.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const base = process.env.UI_BASE_URL || "http://127.0.0.1:8876";
const out = process.env.UI_SCREENSHOT_DIR || "/tmp/hinanjo-if-screenshots";
async function main() {
  await fs.mkdir(out, { recursive: true });
  const browser = await chromium.launch({
    channel: process.env.CHROME_CHANNEL || "chrome",
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
    });
    const errors = [];
    let expectingPdfError = false;
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => {
      if (m.type() === "error" && !expectingPdfError) errors.push(m.text());
    });
    const project = `e2e-backup-${Date.now()}`;
    async function idle() {
      await page.waitForFunction(
        () => !document.body.classList.contains("is-busy"),
        null,
        { timeout: 180000 },
      );
      const error = await page.locator("#notice").getAttribute("class");
      assert(
        !error.includes("error"),
        await page.locator("#notice").innerText(),
      );
    }
    async function request(url) {
      const r = await page.request.get(base + url);
      assert(r.ok(), await r.text());
      return r.json();
    }
    async function shot(name) {
      await page.evaluate(() => {
        window.scrollTo(0, 0);
        document
          .querySelectorAll(".simulation-side,.report-bottom>.panel")
          .forEach((n) => (n.scrollTop = 0));
      });
      await page.waitForTimeout(350);
      await page.screenshot({
        path: path.join(out, `${name}-1440.png`),
        fullPage: true,
      });
      await page.setViewportSize({ width: 1920, height: 1080 });
      await page.waitForTimeout(350);
      await page.screenshot({
        path: path.join(out, `${name}-1920.png`),
        fullPage: true,
      });
      await page.setViewportSize({ width: 1440, height: 900 });
    }
    async function computed(branch, minute) {
      const start = Date.now();
      for (;;) {
        const data = await request(root);
        const ref = data.branches.find((b) => b.branch_id === branch);
        if (data.status !== "running") {
          assert(ref.computed_until >= minute, JSON.stringify(data));
          break;
        }
        assert(Date.now() - start < 180000, "calculation timed out");
        await page.waitForTimeout(750);
      }
      await page.waitForFunction(
        (m) => Number(document.querySelector("#seek").max) >= m,
        minute,
        { timeout: 30000 },
      );
    }
    await page.goto(`${base}/demo-v2?project=${project}`);
    await page.waitForLoadState("networkidle");
    await idle();
    assert(await page.locator("#approve").isDisabled());
    await page.locator("#example").click();
    await page.locator("#duration").fill("0.025");
    await page
      .locator("summary")
      .filter({ hasText: "任意の施設条件・資源" })
      .click();
    await page.locator("#food").fill("300");
    await page.locator("#water").fill("200");
    await page.locator("#preview").click();
    await idle();
    assert(
      (await page.locator("#preview-summary").innerText()).includes("100人"),
    );
    await page.locator("#example").click();
    await page.locator("#municipality").fill("墨田区");
    await page.locator("#disaster").selectOption("地震");
    await page.locator("#evacuees").fill(process.env.UI_POPULATION || "100");
    await page.locator("#duration").fill("0.025");
    await page.locator("#preview").click();
    await idle();
    assert(await page.locator("#preview-result").isVisible());
    const before = await request(`/api/v1/projects/${project}/runs`);
    assert.equal(before.runs.length, 0, "preview must not create Run");
    await page.locator("#scenario-check").check();
    assert(await page.locator("#approve").isEnabled());
    await page.locator("#duration").fill("0.03");
    assert(await page.locator("#approve").isDisabled());
    await page.locator("#duration").fill("0.025");
    await page.locator("#preview").click();
    await idle();
    await shot("scenario");
    await page.locator("#scenario-check").check();
    await page.locator("#approve").click();
    await idle();
    const run = new URL(page.url()).searchParams.get("run");
    assert(run);
    const root = `/api/v1/projects/${project}/runs/${run}`;
    const llm = await request(`${root}/branches/main/llm-status`);
    assert.equal(
      llm.mode,
      "offline",
      "test server must explicitly run offline",
    );
    await page.locator("#target").fill("15");
    await page.locator("#compute").click();
    await idle();
    await computed("main", 15);
    await page.locator("#seek").fill("5");
    await page.locator("#seek").dispatchEvent("change");
    await idle();
    assert((await page.locator("#clock").innerText()).includes("00:05"));
    const parentBefore = (
      await request(`${root}/branches/main/timeline?minute=5`)
    ).state_hash;
    await page.locator("#speed").selectOption("60");
    await page.locator("#play").click();
    await page.waitForFunction(
      () => Number(document.querySelector("#seek").value) > 5,
    );
    await page.locator("#play").click();
    await page.locator("#seek").fill("5");
    await page.locator("#seek").dispatchEvent("change");
    await idle();
    await page.locator('[data-layer="stress"]').click();
    await page.locator('[data-zoom="in"]').first().click();
    await page.locator('[data-zoom="reset"]').first().click();
    await page.locator('[data-layer="normal"]').click();
    await page.locator("#people-list button").first().click();
    assert((await page.locator("#person-detail").innerText()).includes("A001"));
    await shot("simulation");
    await page.locator("#intervention-text").fill("トイレを2基追加する");
    await page.locator("#intervention-preview").click();
    await idle();
    assert(await page.locator("#intervention-apply").isDisabled());
    await page
      .locator("#intervention-result button")
      .filter({ hasText: "トイレ (toilet)" })
      .click();
    await idle();
    assert(
      await page.locator("#intervention-check").isEnabled(),
      await page.locator("#intervention-result").innerText(),
    );
    assert.equal(
      (await request(`${root}/branches/main/timeline?minute=5`)).state_hash,
      parentBefore,
      "preview cannot change parent",
    );
    await page.locator("#intervention-check").check();
    await page
      .locator("#intervention-text")
      .fill("トイレを3基追加する zone_id: toilet");
    assert(await page.locator("#intervention-apply").isDisabled());
    await page.locator("#intervention-preview").click();
    await idle();
    await page.locator("#intervention-check").check();
    await page.locator("#intervention-apply").click();
    await idle();
    const child = new URL(page.url()).searchParams.get("branch");
    assert.notEqual(child, "main");
    assert.equal(
      (await request(`${root}/branches/main/timeline?minute=5`)).state_hash,
      parentBefore,
      "fork cannot change parent",
    );
    await page.locator('[data-page="simulation"]').click();
    await page.locator("#target").fill("36");
    await page.locator('[data-page="comparison"]').click();
    assert.equal(await page.locator("#compare-target").inputValue(), "36");
    await page.locator("#compute-pair").click();
    await idle();
    const compare = await request(
      `${root}/compare?branch_ids=main,${child}&minute=36`,
    );
    assert.equal(compare.frames[0].minute, compare.frames[1].minute);
    assert(compare.shared_origin);
    assert.equal(compare.differences[0].metrics.toilet_working_units, 3);
    assert((await page.locator("#compare-time").innerText()).includes("00:36"));
    await shot("comparison");
    await page.locator("#speed").selectOption("60");
    await page.locator("#play").click();
    await page.waitForFunction(
      () => Number(document.querySelector("#compare-seek").value) > 5,
    );
    await page.locator("#play").click();
    await page.locator("#compare-refresh").click();
    await idle();
    await page.locator('[data-page="simulation"]').click();
    await page.locator("#seek").fill("36");
    await page.locator("#seek").dispatchEvent("change");
    await idle();
    await page.locator('[data-page="report"]').click();
    await idle();
    assert(
      !(await page.locator("#history-note").innerText()).includes(
        "まだありません",
      ),
    );
    const history = await request(
      `${root}/branches/${child}/history?kind=kpis&start=5&end=36&limit=100`,
    );
    assert(history.items.length > 1);
    console.log("KPI history keys", Object.keys(history.items[0]));
    const report = await request(`${root}/branches/${child}/report`);
    assert.notEqual(report.status, "not_generated");
    await page.locator("#question").fill("なぜトイレが混雑したのか");
    await page.locator("#ask").click();
    await idle();
    assert((await page.locator("#chat").innerText()).includes("API未接続"));
    assert(
      (await page.locator("#chat").innerText()).includes("回答できません"),
    );
    await shot("report");
    // A fresh offline run must show a retryable error, rather than exporting JSON
    // or presenting deterministic text as an Opus-authored PDF.
    const pdfResponse = page.waitForResponse((r) => r.url().endsWith("/pdf-report"));
    let downloads = 0;
    page.on("download", () => downloads++);
    expectingPdfError = true;
    await page.locator("#save-report").click();
    assert.equal((await pdfResponse).status(), 409);
    await page.waitForFunction(() => !document.body.classList.contains("is-busy"));
    expectingPdfError = false;
    assert((await page.locator("#notice").innerText()).includes("pdf_analysis_offline"));
    assert.equal(await page.locator("#save-report").isEnabled(), true);
    assert.equal((await page.locator("#save-report").innerText()).trim(), "↓ レポート保存");
    assert.equal(downloads, 0);
    await page.reload();
    await page.waitForLoadState("networkidle");
    await idle();
    assert.equal(new URL(page.url()).searchParams.get("branch"), child);
    for (const width of [1440, 1920, 800, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(150);
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth + 1,
        ),
        `horizontal overflow at ${width}`,
      );
    }
    assert.deepEqual(errors, []);
    console.log(
      JSON.stringify({
        result: "PASS",
        project,
        run,
        child,
        screenshots: out,
        report_status: report.status,
        checks:
          "scenario approval, invalidation, computation, playback, seek, layers, selection, intervention clarification and approval, parent isolation, synchronized compare, report, history, offline Q&A, retryable offline PDF error without JSON download, restore, responsive widths",
      }),
    );
  } finally {
    await browser.close();
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
