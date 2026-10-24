"""Capture exact desktop viewports and check the recorded-map UI in real Chrome.

Requires Python Playwright and installed Chrome. No API mocks are used.
--exercise-preview and --ask-live-question explicitly opt into server-side LLM work;
all other operations read existing Run artifacts without advancing either branch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8876")
    parser.add_argument("--project", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--branch", required=True, help="Child branch for paired comparison")
    parser.add_argument(
        "--minute", type=int, help="Common computed minute; defaults to common head"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--preview-project", help="Dedicated QA project, never a user's working project"
    )
    parser.add_argument("--exercise-preview", action="store_true")
    parser.add_argument("--ask-live-question", action="store_true")
    args = parser.parse_args()
    if args.exercise_preview and not args.preview_project:
        parser.error("--exercise-preview requires --preview-project")
    args.output.mkdir(parents=True, exist_ok=True)
    base = args.base_url.rstrip("/")
    root = f"/api/v1/projects/{args.project}/runs/{args.run}"
    metrics: list[dict] = []
    errors: list[str] = []
    checks: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def get(path: str) -> dict:
            response = page.request.get(base + path)
            assert response.ok, f"GET {path}: HTTP {response.status}"
            return response.json()

        def idle() -> None:
            page.wait_for_function(
                "() => !document.body.classList.contains('is-busy')", timeout=240_000
            )
            assert "error" not in (page.locator("#notice").get_attribute("class") or ""), (
                page.locator("#notice").inner_text()
            )

        def capture(name: str) -> None:
            page.wait_for_timeout(2000)  # Allow the transient completion toast to leave.
            for width, height in ((1440, 900), (1920, 1080)):
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("window.scrollTo(0,0)")
                page.evaluate(
                    "document.activeElement instanceof HTMLCanvasElement && "
                    "document.activeElement.blur()"
                )
                page.wait_for_timeout(350)
                row = page.evaluate("""() => ({
                    page: document.querySelector('.page.active').id,
                    width: innerWidth, height: innerHeight,
                    scrollWidth: document.documentElement.scrollWidth,
                    scrollHeight: document.documentElement.scrollHeight,
                    map: (() => {const c=document.querySelector('.page.active canvas');
                      if(!c)return null;const r=c.getBoundingClientRect();
                      return {width:r.width,height:r.height,top:r.top};})()
                })""")
                page.screenshot(path=str(args.output / f"{name}-{width}.png"), full_page=False)
                metrics.append(row)
                assert row["scrollWidth"] <= width + 1, f"Horizontal overflow: {row}"
                assert row["scrollHeight"] <= height + 1, f"Desktop content clipped: {row}"
            page.set_viewport_size({"width": 1440, "height": 900})

        scenario_project = args.preview_project or args.project
        page.goto(f"{base}/demo-v2?project={scenario_project}#scenario")
        page.wait_for_load_state("networkidle")
        idle()
        if args.exercise_preview:
            page.locator("#scenario-text").fill(
                "墨田区で地震が発生。避難者100人、36分間。"
                "高齢者や子ども連れの支援ニーズも確認したい。"
            )
            for selector, value in (
                ("#municipality", "墨田区"),
                ("#evacuees", "100"),
                ("#duration", "0.025"),
            ):
                page.locator(selector).fill(value)
            page.locator("#disaster").select_option(label="地震")
            page.locator("#facility-type").select_option(label="学校（体育館）")
            page.locator("#preview").click()
            idle()
            assert page.locator("#preview-result").is_visible()
        capture("scenario")

        run = get(root)
        child = next(b for b in run["branches"] if b["branch_id"] == args.branch)
        parent_id = child.get("parent_branch_id") or "main"
        parent = next(b for b in run["branches"] if b["branch_id"] == parent_id)
        minute = (
            args.minute
            if args.minute is not None
            else min(parent["computed_until"], child["computed_until"])
        )
        assert max(parent["fork_minute"], child["fork_minute"]) <= minute
        assert minute <= min(parent["computed_until"], child["computed_until"])
        page.goto(
            f"{base}/demo-v2?project={args.project}&run={args.run}&branch={parent_id}#simulation"
        )
        page.wait_for_load_state("networkidle")
        idle()
        assert page.locator("#page-simulation").get_attribute("class") == "page active"
        page.locator("#seek").fill(str(minute))
        page.locator("#seek").dispatch_event("change")
        idle()
        frame = get(f"{root}/branches/{parent_id}/timeline?minute={minute}")
        page.locator("[data-scene-mode]").first.select_option("physical")
        renderer = page.evaluate(
            """frame => {
          const source=JSON.stringify(frame), visible=document.querySelector('#simulation-map');
          const w=visible.clientWidth,h=visible.clientHeight;
          const canvas=document.createElement('canvas');canvas.width=w;canvas.height=h;
          const sf={ctx:canvas.getContext('2d'),w,h};
          const a=window.HinanjoMap.draw(sf,frame,{zoom:1,layer:'normal'});
          const b=window.HinanjoMap.draw(sf,frame,{zoom:1,layer:'normal',pan:{x:31,y:17}});
          const person=a.find(x=>x.type==='person'&&frame.agents.some(p=>p.agent_id===x.id));
          const physical=frame.agents.find(x=>x.agent_id===person.id).point;
          const scale=Math.min((w-44)/frame.geometry.width_m,(h-44)/frame.geometry.height_m);
          const expected={x:(w-frame.geometry.width_m*scale)/2+physical.x*scale,
            y:(h-frame.geometry.height_m*scale)/2+physical.y*scale};
          const moved=b.find(x=>x.id===person.id&&x.type==='person');
          const expanded=structuredClone(frame);expanded.geometry.width_m*=1.5;
          const viewportBounds={width:expanded.geometry.width_m,height:frame.geometry.height_m};
          const sharedA=window.HinanjoMap.draw(sf,frame,{viewportBounds});
          const sharedB=window.HinanjoMap.draw(sf,expanded,{viewportBounds});
          const pa=sharedA.find(x=>x.id===person.id),pb=sharedB.find(x=>x.id===person.id);
          return {unchanged:source===JSON.stringify(frame),person,
            sharedScale:pa.x===pb.x&&pa.y===pb.y,
            expected,pan:{x:moved.x-person.x,y:moved.y-person.y},
            ids:a.filter(x=>x.type==='person').flatMap(x=>x.ids||[x.id]),
            physicalIds:frame.agents.filter(x=>x.point&&x.position_kind!=='display_only').map(x=>x.agent_id)};
        }""",
            frame,
        )
        assert renderer["unchanged"]
        assert renderer["sharedScale"]
        assert abs(renderer["person"]["x"] - renderer["expected"]["x"]) < 0.1
        assert abs(renderer["person"]["y"] - renderer["expected"]["y"]) < 0.1
        assert abs(renderer["pan"]["x"] - 31) < 0.00001
        assert abs(renderer["pan"]["y"] - 17) < 0.00001
        assert set(renderer["physicalIds"]).issubset(renderer["ids"])
        page.locator('[data-layer="stress"]').click()
        assert page.locator("#layer").input_value() == "stress"
        page.locator('[data-layer="normal"]').click()
        rect = page.locator("#simulation-map").bounding_box()
        page.mouse.click(rect["x"] + renderer["person"]["x"], rect["y"] + renderer["person"]["y"])
        assert renderer["person"]["id"] in page.locator("#person-detail").inner_text()
        page.locator("#simulation-map").press("Escape")
        original = page.locator("#simulation-map").evaluate("c=>c.toDataURL()")
        page.locator('[data-zoom="in"]').first.click()
        assert original != page.locator("#simulation-map").evaluate("c=>c.toDataURL()")
        page.locator("#simulation-map").press("Home")
        assert original == page.locator("#simulation-map").evaluate("c=>c.toDataURL()")
        checks["map"] = (
            "physical coordinates, grouped IDs, immutable input, "
            "layer tabs, person click, zoom reset"
        )
        diagram = page.evaluate(
            """frame => {
          const canvas=document.createElement('canvas');canvas.width=817;canvas.height=445;
          const source=JSON.stringify(frame),projected=window.HinanjoScene.project(frame);
          const hits=window.HinanjoMap.draw({ctx:canvas.getContext('2d'),w:817,h:445},
            frame,{mode:'schematic'});
          const people=new Set(frame.agents.map(a=>a.agent_id));
          return {unchanged:source===JSON.stringify(frame),
            count:hits.filter(h=>h.type==='person'&&people.has(h.id)).length,
            individual:hits.filter(h=>h.type==='person'&&people.has(h.id)).every(h=>h.ids.length===1),
            geometry:projected.recorded_geometry===frame.geometry};
        }""",
            frame,
        )
        assert diagram["unchanged"] and diagram["geometry"] and diagram["individual"]
        assert diagram["count"] == len(frame["agents"])
        checks["schematic"] = diagram
        page.locator("[data-scene-mode]").first.select_option("schematic")
        capture("simulation")

        page.locator('[data-page="comparison"]').click()
        page.locator("#compare-branch").select_option(args.branch)
        page.locator("#compare-refresh").click()
        idle()
        page.locator("#compare-seek").fill(str(minute))
        page.locator("#compare-seek").dispatch_event("change")
        idle()
        before = [
            page.locator(f"#compare-map-{n}").evaluate("c=>c.toDataURL()") for n in ("a", "b")
        ]
        rect = page.locator("#compare-map-a").bounding_box()
        page.mouse.move(rect["x"] + 100, rect["y"] + 100)
        page.mouse.down()
        page.mouse.move(rect["x"] + 135, rect["y"] + 120, steps=5)
        page.mouse.up()
        after = [page.locator(f"#compare-map-{n}").evaluate("c=>c.toDataURL()") for n in ("a", "b")]
        assert all(a != b for a, b in zip(before, after, strict=True))
        page.locator("#compare-map-a").press("Home")
        assert before == [
            page.locator(f"#compare-map-{n}").evaluate("c=>c.toDataURL()") for n in ("a", "b")
        ]
        assert (
            frame["state_hash"]
            == get(f"{root}/branches/{parent_id}/timeline?minute={minute}")["state_hash"]
        )
        checks["comparison"] = "common time, synchronized pan/reset, parent state hash preserved"
        capture("comparison")

        page.locator('[data-page="simulation"]').click()
        page.locator("#branch-select").select_option(args.branch)
        idle()
        page.locator("#seek").fill(str(child["computed_until"]))
        page.locator("#seek").dispatch_event("change")
        idle()
        page.locator('[data-page="report"]').click()
        idle()
        assert page.locator("#chart-metric").input_value() == "overview"
        assert page.locator(".chat-intro").is_visible()
        if args.ask_live_question:
            page.locator("#question").fill(
                "この時刻の平均ストレスとトイレ待機人数を、記録上の数値と出典付きで教えてください。"
                "記録された事実と仮説は分けてください。"
            )
            with page.expect_response(
                lambda r: "/questions" in r.url and r.request.method == "POST"
            ) as pending:
                page.locator("#ask").click()
            response = pending.value
            assert response.ok
            answer = response.json()
            idle()
            assert answer.get("mode") == "record"
            assert answer.get("facts") and answer.get("sources")
            checks["live_question"] = {
                "outcome": answer.get("usage", {}).get("outcome"),
                "sources": len(answer["sources"]),
                "facts": len(answer["facts"]),
            }
            page.locator("#chat").evaluate("c=>c.scrollTop=0")
        capture("report")
        report_frame = get(
            f"{root}/branches/{args.branch}/timeline?minute={child['computed_until']}"
        )
        hit = page.evaluate(
            """frame=>{
          const c=document.querySelector('#report-map'),r=c.getBoundingClientRect();
          return window.HinanjoMap.draw({ctx:c.getContext('2d'),w:r.width,h:r.height},frame,
            {mode:'schematic',layer:'normal'}).find(h=>h.type==='person'&&frame.agents.some(a=>a.agent_id===h.id));
        }""",
            report_frame,
        )
        box = page.locator("#report-map").bounding_box()
        page.mouse.click(box["x"] + hit["x"], box["y"] + hit["y"])
        assert hit["id"] in page.locator(".map-inspector").inner_text()
        before = page.locator("#report-map").evaluate("c=>c.toDataURL()")
        page.locator("#report-layer").select_option("stress")
        assert before != page.locator("#report-map").evaluate("c=>c.toDataURL()")
        checks["report_map"] = "person click and recorded stress layer"
        page.reload()
        page.wait_for_load_state("networkidle")
        idle()
        assert page.locator("#page-report").get_attribute("class") == "page active"
        assert page.locator(".chat-intro").is_visible()
        checks["restore"] = "report URL restored, useful chat empty state"
        for view in ("scenario", "simulation", "comparison", "report"):
            page.locator(f'[data-page="{view}"]').click()
            idle()
            for width in (800, 390):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(100)
                assert page.evaluate(
                    "() => document.documentElement.scrollWidth <= innerWidth+1"
                ), (view, width)
            page.set_viewport_size({"width": 1440, "height": 900})
        assert not errors, errors
        result = {"metrics": metrics, "errors": errors, "checks": checks}
        (args.output / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
