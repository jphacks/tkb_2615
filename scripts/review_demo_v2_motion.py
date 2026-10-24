"""Exercise real recorded playback, frozen pause, synchronized A/B and browser CPU time.

Reads an existing computed Run only. Requires Python Playwright and installed Chrome.
CPU ratio is the Chrome page's TaskDuration / wall time, not whole-machine CPU usage.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8878")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    root = f"/api/v1/projects/{args.project}/runs/{args.run}"
    results = {}
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/demo-v2?project={args.project}&run={args.run}&branch=main#simulation")
        page.wait_for_load_state("networkidle")

        def idle():
            page.wait_for_function("() => !document.body.classList.contains('is-busy')")

        def seek(minute):
            page.locator("#seek").fill(str(minute))
            page.locator("#seek").dispatch_event("change")
            idle()

        def pixels(selector):
            data = page.locator(selector).evaluate("c=>c.toDataURL()")
            return hashlib.sha256(data.encode()).hexdigest()

        def task_duration():
            return next(
                m["value"]
                for m in cdp.send("Performance.getMetrics")["metrics"]
                if m["name"] == "TaskDuration"
            )

        idle()
        seek(0)
        cdp = page.context.new_cdp_session(page)
        cdp.send("Performance.enable")
        start_cpu = task_duration()
        start = time.monotonic()
        page.locator("#speed").select_option("1")
        page.locator("#play").click()
        page.wait_for_timeout(1400)
        first = pixels("#simulation-map")
        page.locator("#simulation-map").screenshot(path=str(args.output / "playing-a.png"))
        page.wait_for_timeout(180)
        second = pixels("#simulation-map")
        page.locator("#simulation-map").screenshot(path=str(args.output / "playing-b.png"))
        assert first != second, "Playback must continuously animate, including idle poses"
        page.wait_for_timeout(3500)
        stats = page.evaluate("window.HinanjoPlayback")
        results["simulation"] = {
            **stats,
            "page_cpu_ratio": (task_duration() - start_cpu) / (time.monotonic() - start),
        }
        assert stats["fps"] >= 30, stats
        page.locator("#play").click()
        paused = pixels("#simulation-map")
        clock = page.locator("#clock").inner_text()
        page.wait_for_timeout(1000)
        assert paused == pixels("#simulation-map"), "Pause must freeze both pose and position"
        assert clock == page.locator("#clock").inner_text()
        page.locator("#play").click()
        page.wait_for_timeout(400)
        assert paused != pixels("#simulation-map"), "Resume must continue animation"
        page.locator("#play").click()
        seek(2)
        assert page.locator("#clock").inner_text() == "1日目 00:02"
        page.locator("#speed").select_option("5")
        page.locator("#play").click()
        page.wait_for_function("() => document.querySelector('#clock').textContent!=='1日目 00:02'")
        page.locator("#play").click()
        assert int(page.locator("#seek").input_value()) >= 7
        seek(0)
        results["controls"] = "continuous frames, frozen pause/clock, resume, rewind, speed change"

        # Inspect consecutive real recorded frames for safe route-backed interpolation.
        records = []
        for minute in range(10):
            r = page.request.get(base + root + f"/branches/main/timeline?minute={minute}")
            assert r.ok
            records.append(r.json())
        routes = page.evaluate(
            """frames => {
          let physical=0,schematic=0;
          for(let i=1;i<frames.length;i++)for(const mode of ['physical','schematic']){
            const sample=window.HinanjoScene.sample(frames[i-1],frames[i],.5,mode);
            const count=sample.agents.filter(a=>a._walking).length;
            if(mode==='physical')physical+=count;else schematic+=count;
          }
          return {physical,schematic,people:frames[0].people.length};
        }""",
            records,
        )
        assert routes["physical"] > 0 and routes["schematic"] > 0, routes
        results["recorded_walking_samples"] = routes

        page.locator('[data-page="comparison"]').click()
        page.locator("#compare-branch").select_option(args.branch)
        page.locator("#compare-refresh").click()
        idle()
        page.locator("#compare-seek").fill("6")
        page.locator("#compare-seek").dispatch_event("change")
        idle()
        page.locator("#speed").select_option("1")
        page.locator("#play").click()
        page.wait_for_timeout(2200)
        before = [pixels("#compare-map-a"), pixels("#compare-map-b")]
        page.wait_for_timeout(180)
        after = [pixels("#compare-map-a"), pixels("#compare-map-b")]
        assert all(a != b for a, b in zip(before, after, strict=True)), "Both maps must animate"
        sync = page.evaluate("window.HinanjoPlayback")
        results["comparison"] = sync
        assert sync["comparison"] and sync["fps"] >= 30
        page.locator("#play").click()
        paused = [pixels("#compare-map-a"), pixels("#compare-map-b")]
        page.wait_for_timeout(600)
        assert paused == [pixels("#compare-map-a"), pixels("#compare-map-b")]
        page.screenshot(path=str(args.output / "comparison-paused.png"))
        assert not errors, errors
        results["errors"] = errors
        (args.output / "performance.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(json.dumps(results, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
