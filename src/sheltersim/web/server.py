"""Loopback-only static UI and read-only saved-run HTTP API."""

from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sheltersim.storage.projects import ProjectStore, StorageError
from sheltersim.web.demo import DemoConflictError, DemoError, DemoService, load_demo_scenario
from sheltersim.web.inspector import InspectorService, WebQueryError
from sheltersim.web.jobs import DemoJobError, DemoJobManager
from sheltersim.web.projects import ProjectAPI
from sheltersim.web.results_chat import ResultsChatService

_STATIC = Path(__file__).with_name("static")


def serve_inspector(
    runs_directory: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    demo_scenario: str | Path | None = None,
) -> None:
    """Serve the saved-run Inspector and packaged offline demo until interrupted."""
    service = InspectorService(runs_directory)
    frozen_scenario_path = Path(runs_directory) / ".demo-scenarios" / "frozen.json"
    project_store = ProjectStore(Path(runs_directory) / ".projects")
    demo = (
        DemoService(scenario_storage_path=frozen_scenario_path, usage_store=project_store)
        if demo_scenario is None
        else DemoService(
            load_demo_scenario(demo_scenario),
            scenario_storage_path=frozen_scenario_path,
            usage_store=project_store,
        )
    )
    job_manager = DemoJobManager(demo, Path(runs_directory) / ".demo-jobs")
    projects = ProjectAPI(project_store)
    handler = _handler_for(service, demo, job_manager, projects)
    server = HTTPServer((host, port), handler)
    print(f"ShelterSim inspector: http://{host}:{server.server_port}/")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        job_manager.close()
        projects.close()


def _handler_for(
    service: InspectorService,
    demo: DemoService | None = None,
    jobs: DemoJobManager | None = None,
    projects: ProjectAPI | None = None,
) -> type[BaseHTTPRequestHandler]:
    demo_service = demo or DemoService(usage_store=projects.store if projects else None)
    results_chat = ResultsChatService(service, demo_service, projects)

    class InspectorHandler(BaseHTTPRequestHandler):
        server_version = "ShelterSimInspector/1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP hook name
            parsed = urlsplit(self.path)
            try:
                if parsed.path.startswith("/api/v1/"):
                    if parsed.path == "/api/v1/usage/runtime":
                        if parsed.query:
                            raise StorageError("runtime usage does not accept query parameters")
                        if projects is None:
                            raise StorageError("usage storage is unavailable")
                        from sheltersim.llm.accounting import runtime_usage_summary
                        payload = runtime_usage_summary(projects.store)
                    elif parsed.path == "/api/v1/projects" or parsed.path.startswith("/api/v1/projects/"):
                        if projects is None:
                            raise StorageError("project storage is unavailable")
                        parts = [unquote(p) for p in parsed.path.split("/") if p][3:]
                        payload = projects.get(parts, parse_qs(parsed.query))
                    elif parsed.path == "/api/v1/demo":
                        if parsed.query:
                            raise WebQueryError("demo state does not accept query parameters")
                        payload = demo_service.state()
                    elif parsed.path == "/api/v1/scenario-presets":
                        if parsed.query:
                            raise WebQueryError("scenario presets do not accept query parameters")
                        payload = demo_service.scenario_presets()
                    elif parsed.path == "/api/v1/demo/snapshot":
                        query = parse_qs(parsed.query)
                        if set(query) - {"branch_id", "minute", "agent_id"}:
                            raise WebQueryError("snapshot received unsupported query parameters")
                        branch_id = _string(query, "branch_id", "")
                        if not branch_id:
                            raise WebQueryError("branch_id is required")
                        payload = demo_service.snapshot_view(
                            branch_id,
                            at_minute=_optional_integer(query, "minute"),
                            agent_id=_optional_string(query, "agent_id"),
                        ).to_dict()
                    elif parsed.path == "/api/v1/demo/comparison":
                        query = parse_qs(parsed.query)
                        if set(query) - {"baseline_branch_id", "candidate_branch_id"}:
                            raise WebQueryError("comparison received unsupported query parameters")
                        baseline_branch_id = _string(query, "baseline_branch_id", "")
                        candidate_branch_id = _string(query, "candidate_branch_id", "")
                        if not baseline_branch_id or not candidate_branch_id:
                            raise WebQueryError(
                                "baseline_branch_id and candidate_branch_id are required"
                            )
                        payload = demo_service.comparison_view(
                            baseline_branch_id,
                            candidate_branch_id,
                        )
                    elif parsed.path == "/api/v1/demo/frame":
                        query = parse_qs(parsed.query)
                        if set(query) - {"branch_id", "minute"}:
                            raise WebQueryError("frame received unsupported query parameters")
                        branch_id = _string(query, "branch_id", "")
                        if not branch_id:
                            raise WebQueryError("branch_id is required")
                        payload = demo_service.frame_view(
                            branch_id, at_minute=_optional_integer(query, "minute")
                        )
                    elif parsed.path == "/api/v1/demo/compare-frames":
                        query = parse_qs(parsed.query)
                        if set(query) - {"branch_ids", "minute"}:
                            raise WebQueryError("compare-frames received unsupported query parameters")
                        raw_ids = _string(query, "branch_ids", "")
                        branch_ids = [item for item in raw_ids.split(",") if item]
                        minute = _optional_integer(query, "minute")
                        if not branch_ids or minute is None:
                            raise WebQueryError("branch_ids and minute are required")
                        payload = demo_service.compare_frames(branch_ids, at_minute=minute)
                    elif parsed.path == "/api/v1/demo/precomputed-comparison":
                        if parsed.query:
                            raise WebQueryError(
                                "precomputed comparison does not accept query parameters"
                            )
                        comparison_path = service.runs_directory.parent / "comparison.json"
                        if not comparison_path.is_file():
                            self._json_response(
                                404, {"error": "precomputed demo replay is unavailable"}
                            )
                            return
                        payload = json.loads(comparison_path.read_text(encoding="utf-8"))
                        if (
                            not isinstance(payload, dict)
                            or payload.get("execution_mode") != "precomputed_replay"
                        ):
                            raise WebQueryError("precomputed comparison has an invalid format")
                    elif parsed.path == "/api/v1/jobs":
                        if parsed.query or jobs is None:
                            raise WebQueryError("job list is unavailable or received a query")
                        payload = jobs.list()
                    elif parsed.path.startswith("/api/v1/jobs/"):
                        if parsed.query:
                            raise WebQueryError("job status does not accept query parameters")
                        job_id = unquote(parsed.path.removeprefix("/api/v1/jobs/"))
                        if not job_id or "/" in job_id or jobs is None:
                            raise WebQueryError("unknown API route")
                        payload = jobs.get(job_id)
                    else:
                        payload = _dispatch_api(service, parsed.path, parse_qs(parsed.query))
                    self._json_response(200, payload)
                elif parsed.path == "/":
                    target = "/demo"
                    if parsed.query:
                        target = f"{target}?{parsed.query}"
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                elif parsed.path in {"/demo", "/admin"}:
                    self._static_response(parsed.path)
                else:
                    self._static_response(parsed.path)
            except StorageError as exc:
                self._json_response(409, {"error": str(exc), "code": exc.code})
            except DemoJobError as exc:
                status = 404 if str(exc) == "unknown job_id" else 409
                self._json_response(status, {"error": str(exc)})
            except DemoError as exc:
                status = 404 if "unknown branch_id" in str(exc) else 409
                self._json_response(status, {"error": str(exc)})
            except WebQueryError as exc:
                self._json_response(400, {"error": str(exc)})
            except ValueError:
                self._json_response(400, {"error": "invalid query or saved contract", "code": "invalid_input"})
            except Exception:
                self._json_response(500, {"error": "inspector request failed"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook name
            parsed = urlsplit(getattr(self, "path", ""))
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            origin = self.headers.get("Origin")
            if (origin and urlsplit(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._json_response(403, {"error": "cross-origin mutations are forbidden"})
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json_response(415, {"error": "mutations require application/json"})
                return
            if parsed.query:
                self._json_response(405, {"error": "inspector API is read-only"})
                return
            if parts == ["api", "v1", "results-chat"]:
                try:
                    origin = self.headers.get("Origin")
                    if (origin and urlsplit(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                        raise WebQueryError("cross-origin result questions are forbidden")
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise WebQueryError("result questions require application/json")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 32768:
                        raise WebQueryError("result question exceeds body limit")
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise WebQueryError("result question must be an object")
                    self._json_response(200, results_chat.ask(body))
                except (ValueError, UnicodeDecodeError):
                    self._json_response(400, {"error": "invalid or unavailable result question context"})
                except Exception:
                    self._json_response(409, {"error": "result answer unavailable within the current budget or saved context"})
                return
            if parts[:3] == ["api", "v1", "projects"]:
                try:
                    if projects is None:
                        raise StorageError("project storage is unavailable")
                    origin = self.headers.get("Origin")
                    if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                        raise StorageError("cross-origin project mutation is forbidden")
                    if self.headers.get("Sec-Fetch-Site") == "cross-site":
                        raise StorageError("cross-site project mutation is forbidden")
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise StorageError("project mutations require application/json")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2_097_152:
                        raise StorageError("project request exceeds the body limit")
                    document = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(document, dict):
                        raise StorageError("project request must be an object")
                    payload = projects.post(parts[3:], document)
                    self._json_response(200, payload)
                except StorageError as exc:
                    self._json_response(409, {"error": str(exc), "code": exc.code})
                except (ValueError, UnicodeDecodeError):
                    self._json_response(400, {"error": "invalid project request"})
                except Exception:
                    self._json_response(500, {"error": "project operation failed"})
                return
            is_cancel = (
                parts[:3] == ["api", "v1", "jobs"] and len(parts) == 5 and parts[4] == "cancel"
            )
            is_advance_job = parts == ["api", "v1", "demo", "advance-job"]
            is_story_review = parts == ["api", "v1", "demo", "story-review"]
            is_natural_language = (
                len(parts) == 4
                and parts[:3] == ["api", "v1", "nl"]
                and parts[3] in {"interpret", "validate", "apply"}
            )
            is_multi_plan = (
                len(parts) == 4
                and parts[:3] == ["api", "v1", "nl"]
                and parts[3] in {"multi-validate", "multi-apply"}
            )
            if (
                not is_cancel
                and not is_advance_job
                and not is_story_review
                and not is_natural_language
                and not is_multi_plan
                and (parts[:3] != ["api", "v1", "demo"] or len(parts) != 4)
            ):
                self._json_response(405, {"error": "inspector API is read-only"})
                return
            try:
                if is_cancel:
                    if jobs is None:
                        raise DemoJobError("background jobs are unavailable")
                    self._json_response(200, jobs.cancel(parts[3]))
                    return
                length = int(self.headers.get("Content-Length", "0"))
                maximum_length = (
                    131_072 if is_multi_plan else 65_536 if is_natural_language else 16_384
                )
                if length < 0 or length > maximum_length:
                    raise DemoError(f"request body exceeds {maximum_length} bytes")
                raw = self.rfile.read(length) if length else b"{}"
                document = json.loads(raw.decode("utf-8"))
                if not isinstance(document, dict):
                    raise DemoError("request body must be a JSON object")
                if parts == ["api", "v1", "demo", "advance-job"]:
                    if jobs is None:
                        raise DemoJobError("background jobs are unavailable")
                    if set(document) != {"branch_id", "target_minute"}:
                        raise DemoJobError("advance-job requires branch_id and target_minute")
                    self._json_response(
                        202, jobs.submit(document["branch_id"], document["target_minute"])
                    )
                    return
                if parts == ["api", "v1", "demo", "scenario-preview"]:
                    self._json_response(200, demo_service.preview_scenario(document))
                    return
                if parts == ["api", "v1", "demo", "scenario-freeze"]:
                    self._json_response(200, demo_service.freeze_scenario(document))
                    return
                if is_natural_language:
                    action = parts[3]
                    if action == "interpret":
                        payload = demo_service.interpret_natural_language(document)
                    elif action == "validate":
                        payload = demo_service.validate_natural_language_preview(document)
                    else:
                        payload = demo_service.apply_natural_language_preview(document)
                    self._json_response(200, payload)
                    return
                if is_multi_plan:
                    if parts[3] == "multi-validate":
                        payload = demo_service.validate_multi_plan_preview(document)
                    else:
                        payload = demo_service.apply_multi_plan_preview(document)
                    self._json_response(200, payload)
                    return
                if is_story_review:
                    if set(document) != {"baseline_branch_id", "candidate_branch_id"}:
                        raise DemoError(
                            "story-review requires baseline_branch_id and candidate_branch_id"
                        )
                    payload = demo_service.story_review(
                        document["baseline_branch_id"], document["candidate_branch_id"]
                    )
                    self._json_response(200, payload)
                    return
                if parts[:3] != ["api", "v1", "demo"] or len(parts) != 4:
                    self._json_response(405, {"error": "inspector API is read-only"})
                    return
                self._json_response(200, demo_service.dispatch(parts[3], document))
            except DemoJobError as exc:
                status = 404 if str(exc) == "unknown job_id" else 409
                self._json_response(status, {"error": str(exc)})
            except DemoConflictError as exc:
                self._json_response(409, {"error": str(exc), "code": "demo_conflict"})
            except (UnicodeDecodeError, json.JSONDecodeError, DemoError, ValueError) as exc:
                self._json_response(400, {"error": str(exc)})
            except Exception:
                self._json_response(500, {"error": "demo request failed"})

        def do_PUT(self) -> None:  # noqa: N802 - stdlib HTTP hook name
            self._json_response(405, {"error": "inspector API is read-only"})

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib HTTP hook name
            self._json_response(405, {"error": "inspector API is read-only"})

        def _json_response(self, status: int, document: Any) -> None:
            payload = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def _static_response(self, request_path: str) -> None:
            if request_path.startswith("/vendor/pdfjs/"):
                relative = request_path.removeprefix("/vendor/pdfjs/")
                root = (_STATIC / "vendor" / "pdfjs").resolve()
                candidate = (root / relative).resolve()
                allowed = (
                    relative in {"pdf.min.mjs", "pdf.worker.min.mjs", "LICENSE", "NOTICE.txt"}
                    or relative.startswith("cmaps/") and Path(relative).suffix in {".bcmap", ".json"}
                    or relative.startswith("standard_fonts/") and Path(relative).suffix in {".pfb", ".ttf"}
                )
                if (not allowed or root not in candidate.parents or not candidate.is_file()):
                    self._json_response(404, {"error": "PDF renderer asset not found"})
                    return
                payload = candidate.read_bytes()
                content_type = "text/javascript" if candidate.suffix == ".mjs" else (
                    "font/ttf" if candidate.suffix == ".ttf" else mimetypes.guess_type(candidate.name)[0]
                ) or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self' data: blob:; worker-src 'self'")
                self.end_headers()
                self.wfile.write(payload)
                return
            names = {
                "/demo-v2": "demo-v2.html",
                "/demo-v2.css": "demo-v2.css",
                "/demo-v2.js": "demo-v2.js",
                "/demo-v2-map.js": "demo-v2-map.js",
                "/demo-v2-scene.js": "demo-v2-scene.js",
                "/demo-v2-clock.js": "demo-v2-clock.js",
                "/demo-v2-replay.js": "demo-v2-replay.js",
                "/demo-v2-sprites.js": "demo-v2-sprites.js",
                "/index.html": "index.html",
                "/project.js": "project.js",
                "/project-round3.js": "project-round3.js",
                "/project.css": "project.css",
                "/performance.js": "performance.js",
                "/map-lod.js": "map-lod.js",
                "/demo": "index.html",
                "/admin": "index.html",
                "/app.js": "app.js",
                "/style.css": "style.css",
            }
            filename = names.get(request_path)
            if filename is None:
                self._json_response(404, {"error": "page not found"})
                return
            payload = (_STATIC / filename).read_bytes()
            if request_path in {"/demo", "/admin"}:
                mode = "admin" if request_path == "/admin" else "demo"
                payload = payload.replace(b'data-mode="demo"', f'data-mode="{mode}"'.encode())
            self.send_response(200)
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data: blob:")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            # Keep routine local queries quiet; malformed/failed requests still get HTTP errors.
            del format, args

    return InspectorHandler


def _dispatch_api(
    service: InspectorService,
    request_path: str,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    parts = [unquote(part) for part in request_path.split("/") if part]
    if parts == ["api", "v1", "runs"]:
        return service.list_runs()
    if len(parts) < 4 or parts[:3] != ["api", "v1", "runs"]:
        raise WebQueryError("unknown API route")
    run_id = parts[3]
    if len(parts) == 4:
        return service.overview(run_id)
    if len(parts) == 5 and parts[4] == "profiles":
        return service.profiles(
            run_id,
            query=_string(query, "q", ""),
            offset=_integer(query, "offset", 0),
            limit=_integer(query, "limit", 100),
        )
    if len(parts) == 5 and parts[4] == "agent-stories":
        if set(query) - {"at_minute"}:
            raise WebQueryError("agent-stories received unsupported query parameters")
        return service.agent_stories(
            run_id,
            at_minute=_optional_integer(query, "at_minute"),
        )
    if len(parts) == 5 and parts[4] == "resource-ledger":
        return service.resource_ledger(run_id)
    if len(parts) == 5 and parts[4] == "events":
        return service.events(
            run_id,
            start_minute=_optional_integer(query, "start_minute"),
            end_minute=_optional_integer(query, "end_minute"),
            query=_string(query, "q", ""),
            branch_id=_optional_string(query, "branch_id"),
            agent_id=_optional_string(query, "agent_id"),
            zone_id=_optional_string(query, "zone_id"),
            status=_string(query, "status", "all"),
            offset=_integer(query, "offset", 0),
            limit=_integer(query, "limit", 100),
        )
    if len(parts) == 5 and parts[4] == "decisions":
        return service.decisions(
            run_id,
            agent_id=_optional_string(query, "agent_id"),
            start_minute=_optional_integer(query, "start_minute"),
            end_minute=_optional_integer(query, "end_minute"),
            at_minute=_optional_integer(query, "at_minute"),
            limit=_integer(query, "limit", 100),
        )
    if len(parts) == 5 and parts[4] == "branch-lineage":
        if query:
            raise WebQueryError("branch-lineage does not accept query parameters")
        return service.branch_lineage(run_id)
    if len(parts) == 6 and parts[4] == "events":
        return service.event(run_id, parts[5])
    if len(parts) == 6 and parts[4] == "agents":
        return service.agent(
            run_id,
            parts[5],
            at_minute=_optional_integer(query, "at_minute"),
            start_minute=_optional_integer(query, "start_minute"),
            end_minute=_optional_integer(query, "end_minute"),
        )
    if len(parts) == 5 and parts[4] == "timeline":
        return service.timeline(
            run_id,
            start_minute=_optional_integer(query, "start_minute"),
            end_minute=_optional_integer(query, "end_minute"),
            at_minute=_optional_integer(query, "at_minute"),
            limit=_integer(query, "limit", 1000),
        )
    if len(parts) == 5 and parts[4] == "map":
        return service.zone_map(
            run_id,
            at_minute=_optional_integer(query, "at_minute"),
            selected_agent_id=_optional_string(query, "agent_id"),
        )
    if len(parts) == 5 and parts[4] == "social-contacts":
        return service.social_contacts(
            run_id,
            start_minute=_optional_integer(query, "start_minute"),
            end_minute=_optional_integer(query, "end_minute"),
            limit=_integer(query, "limit", 100),
        )
    if len(parts) == 5 and parts[4] == "audit":
        return service.audit(run_id, limit=_integer(query, "limit", 100))
    raise WebQueryError("unknown API route")


def _string(query: dict[str, list[str]], key: str, default: str) -> str:
    value = query.get(key, [default])[0]
    if len(value) > 256:
        raise WebQueryError(f"{key} is too long")
    return value


def _optional_string(query: dict[str, list[str]], key: str) -> str | None:
    return _string(query, key, "") or None


def _integer(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = query.get(key, [str(default)])[0]
    try:
        return int(raw)
    except ValueError as exc:
        raise WebQueryError(f"{key} must be an integer") from exc


def _optional_integer(query: dict[str, list[str]], key: str) -> int | None:
    raw = query.get(key, [None])[0]
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise WebQueryError(f"{key} must be an integer") from exc
