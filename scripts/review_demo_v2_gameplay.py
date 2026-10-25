"""Verify recorded on-screen translation and gait, not just changing pixels or RAF.

Read-only against an existing Run. --record makes an actual Chrome screencast MP4
using ffmpeg; no simulated intermediate image frames or LLM calls are generated.
"""

import argparse
import base64
import hashlib
import json
import subprocess
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
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    root = f"/api/v1/projects/{args.project}/runs/{args.run}"
    result = {}
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{base}/demo-v2?project={args.project}&run={args.run}&branch=main#simulation")
        page.wait_for_load_state("networkidle")

        def idle():
            page.wait_for_function(
                "() => !document.body.classList.contains('is-busy')", timeout=180000
            )

        def seek(minute):
            page.locator("#seek").fill(str(minute))
            page.locator("#seek").dispatch_event("change")
            idle()

        def snapshot(selector="#simulation-map"):
            return page.evaluate(
                """selector=>({view:window.HinanjoReplayView,
              actors:window.HinanjoMap.inspect(document.querySelector(selector)).filter(a=>a.type==='person')})""",
                selector,
            )

        def pixels(selector):
            return hashlib.sha256(
                page.locator(selector).evaluate("c=>c.toDataURL()").encode()
            ).hexdigest()

        idle()
        records = []
        for minute in range(13):
            response = page.request.get(base + root + f"/branches/main/timeline?minute={minute}")
            assert response.ok
            records.append(response.json())
        (args.output / "recorded-frames.json").write_text(json.dumps(records, ensure_ascii=False))
        result["recorded_audit"] = page.evaluate(
            """frames=>{
          const moving=new Set(),zones=new Set(),routes=new Set();
          const interpolated=new Set(),fallback=new Set();
          const intervals=[];let staffChanges=0;
          for(let i=1;i<frames.length;i++){
            const a=frames[i-1],b=frames[i],plan=window.HinanjoScene.prepare(a,b,'schematic');
            for(const p of [...a.people,...b.people])if(p.activity==='moving')moving.add(p.id);
            plan.metrics.changedZones.forEach(id=>zones.add(id));
            plan.metrics.recordedJourneys.forEach(id=>routes.add(id));
            plan.metrics.interpolated.forEach(id=>interpolated.add(id));
            plan.metrics.fallback.forEach(id=>fallback.add(id));
            const first=plan.sample(.25),second=plan.sample(.75);
            const end=new Map(second.agents.map(a=>[a.agent_id,a]));
            const translated=first.agents.filter(a=>a._walking&&Math.hypot(
              a.point.x-end.get(a.agent_id).point.x,a.point.y-end.get(a.agent_id).point.y)>.001);
            if(translated.length!==plan.metrics.interpolated.length)
              throw new Error('walking without translation');
            if(plan.entries.some(e=>e.path&&!window.HinanjoScene.safeDiagramPath(e.path)))
              throw new Error('diagram wall crossing');
            for(const s of a.staff){const next=b.staff.find(x=>x.id===s.id);
              const p=s.position?.point,q=next?.position?.point;
              if(p&&q&&Math.hypot(p.x-q.x,p.y-q.y)>.0001)staffChanges++;}
            intervals.push({minute:a.minute,changed:plan.metrics.changedPositions.length,
              interpolated:translated.length,fallback:plan.metrics.fallback.length});
          }
          return {population:frames[0].agents.length,
            movingAgents:moving.size,zoneChangedAgents:zones.size,
            recordedRouteAgents:routes.size,interpolatedAgents:interpolated.size,fallbackAgents:fallback.size,
            interpolatedSamples:intervals.reduce((n,r)=>n+r.interpolated,0),
            fallbackSamples:intervals.reduce((n,r)=>n+r.fallback,0),staffPositionChanges:staffChanges,intervals};
        }""",
            records,
        )
        assert result["recorded_audit"]["interpolatedAgents"] > 0
        seek(0)
        page.locator("#speed").select_option("60")
        cdp = page.context.new_cdp_session(page)
        film = []
        if args.record:
            directory = args.output / "screencast"
            directory.mkdir(exist_ok=True)

            def captured(data):
                name = f"f{len(film):05d}.jpg"
                (directory / name).write_bytes(base64.b64decode(data["data"]))
                film.append((name, time.monotonic()))
                cdp.send("Page.screencastFrameAck", {"sessionId": data["sessionId"]})

            cdp.on("Page.screencastFrame", captured)
            cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 85,
                    "maxWidth": 1440,
                    "maxHeight": 900,
                    "everyNthFrame": 2,
                },
            )
        page.locator("#play").click()
        page.wait_for_function("""()=>window.HinanjoReplayView?.fraction>.1&&
          window.HinanjoMap.inspect(document.querySelector('#simulation-map')).some(a=>a.walking)""")
        series = []
        for i in range(7):
            series.append(snapshot())
            page.locator("#simulation-map").screenshot(path=str(args.output / f"walk-{i}.png"))
            page.wait_for_timeout(110)
        moving = {}
        for sample in series:
            assert sample["view"]["minute"] <= sample["view"]["bufferedUntil"]
            for a in sample["actors"]:
                moving.setdefault(a["id"], []).append(a)
        moved = []
        gait = []
        for agent_id, points in moving.items():
            walking = [a for a in points if a["walking"]]
            if len(walking) < 2:
                continue
            displacement = max(
                ((a["x"] - walking[0]["x"]) ** 2 + (a["y"] - walking[0]["y"]) ** 2) ** 0.5
                for a in walking
            )
            if displacement > 3:
                moved.append(agent_id)
            if displacement > 3 and len({a["spriteFrame"] for a in walking}) > 1:
                gait.append(agent_id)
        assert moved and gait, "RAF or breathing alone does not demonstrate walking"
        coordinate_error = page.evaluate(
            """({series,frames})=>{
          const c=document.querySelector('#simulation-map'),r=c.getBoundingClientRect();
          const scale=Math.min((r.width-44)/100,(r.height-44)/66);
          const ox=(r.width-100*scale)/2,oy=(r.height-66*scale)/2;
          let error=0;
          for(const row of series){
            const minute=Math.floor(row.view.minute),t=row.view.minute-minute;
            const sample=window.HinanjoScene.sample(frames[minute],frames[minute+1],t,'schematic');
            const people=new Map(sample.agents.map(a=>[a.agent_id,a]));
            for(const actor of row.actors){const p=people.get(actor.id)?.point;if(!p)continue;
              error=Math.max(error,Math.hypot(actor.x-ox-p.x*scale,actor.y-oy-p.y*scale));}
          }return error;
        }""",
            {"series": series, "frames": records},
        )
        assert coordinate_error < 0.1, (
            "batch playback must match independently read Timeline frames"
        )
        result["on_screen"] = {
            "translatedAgents": len(moved),
            "gaitWithTranslationAgents": len(gait),
            "sampledFrames": len(series),
            "trackedIds": moved,
            "maxRecordedCoordinateErrorPx": coordinate_error,
        }
        (args.output / "screen-coordinates.json").write_text(
            json.dumps(series, ensure_ascii=False, indent=2)
        )
        page.locator("#play").click()
        frozen = pixels("#simulation-map")
        frozen_clock = page.locator("#clock").inner_text()
        page.wait_for_timeout(650)
        assert (
            frozen == pixels("#simulation-map")
            and frozen_clock == page.locator("#clock").inner_text()
        )
        page.locator("#play").click()
        page.wait_for_timeout(300)
        assert frozen != pixels("#simulation-map")
        page.locator("#speed").select_option("120")
        page.wait_for_function(
            "()=>window.HinanjoReplayView.bufferedUntil>window.HinanjoReplayView.minute+3"
        )
        before = snapshot()["view"]["minute"]
        wall = time.monotonic()
        page.wait_for_timeout(1100)
        ratio = (snapshot()["view"]["minute"] - before) * 60000 / ((time.monotonic() - wall) * 1000)
        assert 105 < ratio < 135, ratio
        result["measuredRate120x"] = ratio
        page.locator("#play").click()
        if args.record:
            cdp.send("Page.stopScreencast")
        seek(2)
        page.locator("#speed").select_option("1")
        page.locator("#play").click()
        page.wait_for_function(
            "()=>window.HinanjoReplayView.bufferedUntil>window.HinanjoReplayView.minute+1"
        )
        before = snapshot()["view"]["minute"]
        wall = time.monotonic()
        page.wait_for_timeout(1200)
        ratio = (snapshot()["view"]["minute"] - before) * 60000 / ((time.monotonic() - wall) * 1000)
        assert 0.85 < ratio < 1.15, ratio
        result["measuredRate1x"] = ratio
        page.locator("#play").click()
        seek(0)

        # Warm sprites and measure without screenshot/recording work in the interval.
        page.locator("#speed").select_option("60")
        page.locator("#play").click()
        page.wait_for_timeout(2400)
        page.wait_for_function("""()=>window.HinanjoReplayView.bufferedUntil>window.HinanjoReplayView.minute+4&&
          window.HinanjoMap.inspect(document.querySelector('#simulation-map')).some(a=>a.walking)""")
        cdp.send("Performance.enable")

        def cpu():
            return next(
                m["value"]
                for m in cdp.send("Performance.getMetrics")["metrics"]
                if m["name"] == "TaskDuration"
            )

        first = cpu()
        first_minute = snapshot()["view"]["minute"]
        wall = time.monotonic()
        page.wait_for_timeout(2200)
        result["performance"] = {
            **page.evaluate("window.HinanjoPlayback"),
            "pageCpuRatio": (cpu() - first) / (time.monotonic() - wall),
        }
        assert result["performance"]["fps"] >= 30
        assert snapshot()["view"]["minute"] - first_minute > 1.5, (
            "FPS must be measured during real playback"
        )
        page.locator("#play").click()
        page.locator('[data-page="comparison"]').click()
        page.locator("#compare-branch").select_option(args.branch)
        page.locator("#compare-refresh").click()
        idle()
        page.locator("#compare-seek").fill("6")
        page.locator("#compare-seek").dispatch_event("change")
        idle()
        page.locator("#play").click()
        page.wait_for_function("""()=>window.HinanjoReplayView?.comparison&&window.HinanjoReplayView.fraction>.1&&
          window.HinanjoMap.inspect(document.querySelector('#compare-map-a')).some(a=>a.walking)""")
        first_a = snapshot("#compare-map-a")
        first_b = snapshot("#compare-map-b")
        page.wait_for_timeout(180)
        next_a = snapshot("#compare-map-a")
        next_b = snapshot("#compare-map-b")
        assert first_a["view"]["from"][0] == first_a["view"]["from"][1]
        assert first_a["view"]["to"][0] == first_a["view"]["to"][1]
        for first, last in ((first_a, next_a), (first_b, next_b)):
            lookup = {a["id"]: a for a in last["actors"]}
            assert any(
                a["walking"]
                and a["id"] in lookup
                and abs(a["x"] - lookup[a["id"]]["x"]) + abs(a["y"] - lookup[a["id"]]["y"]) > 1
                for a in first["actors"]
            )
        page.wait_for_timeout(2000)
        result["comparisonPerformance"] = page.evaluate("window.HinanjoPlayback")
        page.locator("#play").click()
        frozen = [pixels("#compare-map-a"), pixels("#compare-map-b")]
        page.wait_for_timeout(550)
        assert frozen == [pixels("#compare-map-a"), pixels("#compare-map-b")]
        page.screenshot(path=str(args.output / "comparison.png"))
        page.locator('[data-page="report"]').click()
        idle()
        assert all(not a["walking"] for a in snapshot("#report-map")["actors"])
        assert not page.evaluate("window.HinanjoReplayView.playing")
        result["controls"] = (
            "frozen pause, resume, 1x/120x speed, seek, "
            "one A/B clock, two translated maps, static report"
        )
        assert not errors, errors
        result["errors"] = errors
        (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        browser.close()
    if args.record and film:
        lines = []
        for i, (name, stamp) in enumerate(film):
            duration = film[i + 1][1] - stamp if i + 1 < len(film) else 1 / 30
            lines.extend([f"file {name}", f"duration {max(0.001, duration):.6f}"])
        concat = args.output / "screencast" / "frames.ffconcat"
        concat.write_text("\n".join(lines) + "\n")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-vf",
                "fps=30,format=yuv420p",
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                str(args.output / "recorded-playback.mp4"),
            ],
            check=True,
        )
        result["recordingFrames"] = len(film)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
