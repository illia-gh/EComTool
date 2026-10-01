"""FastAPI backend for EComTool one-click local Python workflow.

Frontend is a single static page. Legacy granular endpoints remain available for
tests and reference workflows; user UI calls /api/analyze.

Run:
    python -m uvicorn app:app --app-dir tool/scripts --port 8000
    # then open http://127.0.0.1:8000
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config_schema
import pipeline
import report as report_mod

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUTPUTS = REPO / "outputs"
OUTPUTS.mkdir(exist_ok=True)

app = FastAPI(title="EComTool admin")

# in-memory job store: job_id -> {status, stage, rc, log}
JOBS: dict[str, dict] = {}
PIPELINE_JOB_LOCK = threading.Lock()


def report_path(mode: str) -> Path:
    pipeline.model(mode)
    return OUTPUTS / f"report_{mode.lower()}.html"


class ModeReq(BaseModel):
    mode: str


class RunReq(BaseModel):
    mode: str
    config: dict


class ValidateReq(BaseModel):
    mode: str
    config: dict


class ReportReq(BaseModel):
    mode: str
    sections: list[str] = list(report_mod.ALL_SECTIONS)


class AnalyzeReq(BaseModel):
    mode: str
    config: dict
    sections: list[str] = list(report_mod.ALL_SECTIONS)
    cold_stage1: bool = False


class CacheCleanupReq(BaseModel):
    keep: int = Field(default=pipeline.CACHE_RETENTION_DEFAULT, ge=1, le=100)
    dry_run: bool = True


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (HERE.parent / "index.html").read_text(encoding="utf-8")


@app.get("/favicon.ico")
def favicon() -> Response:
    """Silence the browser's automatic /favicon.ico probe (the page embeds an inline icon)."""
    return Response(status_code=204)


@app.get("/api/schema/{mode}")
def get_schema(mode: str):
    try:
        schema = config_schema.ui_schema(mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    defaults = pipeline.default_config(mode)
    fields = {
        k: {"kind": s.kind, "lo": s.lo, "hi": s.hi, "choices": s.choices, "desc": s.desc}
        for k, s in schema.items()
    }
    timings = pipeline.read_run_timings(mode)
    calculation_current = pipeline.calculation_is_current(mode)
    results_current = pipeline.results_are_current(mode)
    report_current = pipeline.report_is_current(mode, report_path(mode))
    display_results = calculation_current or report_current
    export_state = pipeline.result_export_state(mode)
    stage1_export = pipeline.stage1_export_state()
    run_id = timings.get("run_id") if isinstance(timings, dict) else None
    return {"mode": mode, "fields": fields, "defaults": defaults,
            "input_present": pipeline.input_present(mode),
            "user_input_present": pipeline.user_input_present(),
            "results_present": display_results,
            "report_present": report_current,
            "results_export": export_state,
            "stage1_export": stage1_export,
            "artifacts": {
                "stage1": (f"/outputs/runtime/{pipeline.STAGE1_OUTPUT_NAME}"
                           if display_results and pipeline.stage1_is_current() else None),
                "results": (f"/outputs/runtime/{pipeline.results_path(mode).name}"
                            if results_current else None),
                "report": (f"/outputs/{report_path(mode).name}" if report_current else None),
                "log": (f"/outputs/runtime/{pipeline.run_log_path(mode).name}"
                        if pipeline.run_log_path(mode).is_file() else None),
                "timing": (f"/outputs/runtime/{pipeline.timing_path(mode).name}"
                           if pipeline.timing_path(mode).is_file() else None),
                "performance_log": (f"/outputs/runtime/performance/{run_id}.log"
                                    if run_id else None),
                "performance_timing": (f"/outputs/runtime/performance/{run_id}.json"
                                       if run_id else None),
                "timings": timings,
            }}


def _start_job(
    fn, *, streams_log: bool = False, tracks_stage: bool = False,
    exclusive: bool = False,
) -> str:
    """Launch fn() -> (rc, log[, result]) in a daemon thread; return job id."""
    lock_acquired = exclusive and PIPELINE_JOB_LOCK.acquire(blocking=False)
    if exclusive and not lock_acquired:
        raise HTTPException(409, "another Stage-1 or compute job is already running")

    job_id = uuid.uuid4().hex[:8]
    started_at = datetime.now(timezone.utc)
    started_clock = time.perf_counter()
    JOBS[job_id] = {
        "status": "running", "stage": None, "rc": None, "log": "", "result": None,
        "started_at_utc": started_at.isoformat(), "finished_at_utc": None,
        "elapsed_seconds": None,
    }

    def append_log(text: str) -> None:
        JOBS[job_id]["log"] += text

    def set_stage(stage: str) -> None:
        JOBS[job_id]["stage"] = stage

    def worker():
        try:
            if tracks_stage:
                outcome = fn(append_log, set_stage)
            else:
                outcome = fn(append_log) if streams_log else fn()
            rc, log, *extra = outcome
            result = extra[0] if extra else None
            JOBS[job_id].update(
                status="done" if rc == 0 else "failed",
                rc=rc,
                log=log,
                result=result,
            )
        except Exception as e:  # noqa: BLE001
            prior_log = JOBS[job_id]["log"]
            separator = "" if not prior_log or prior_log.endswith("\n") else "\n"
            JOBS[job_id].update(
                status="failed",
                rc=-1,
                log=f"{prior_log}{separator}{type(e).__name__}: {e}",
            )
        finally:
            JOBS[job_id]["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["elapsed_seconds"] = round(time.perf_counter() - started_clock, 3)
            if lock_acquired:
                PIPELINE_JOB_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _exclusive_pipeline_call(fn):
    """Reject report/compute overlap and always release lock after synchronous work."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not PIPELINE_JOB_LOCK.acquire(blocking=False):
            raise HTTPException(409, "Stage-1 or compute is still running")
        try:
            return fn(*args, **kwargs)
        finally:
            PIPELINE_JOB_LOCK.release()

    return wrapped


@app.post("/api/cache-cleanup")
@_exclusive_pipeline_call
def api_cache_cleanup(req: CacheCleanupReq):
    """Preview or remove old runtime artifacts while analysis is idle."""
    return pipeline.cleanup_runtime_cache(keep=req.keep, dry_run=req.dry_run)


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        path = pipeline.save_user_input(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            409,
            f"Could not save {pipeline.USER_INPUT_NAME}: file temporarily locked "
            f"(antivirus/indexer or another program holding it) — retry in a moment: {exc}",
        ) from exc
    return {"ok": True, "saved": pipeline.USER_INPUT_NAME, "bytes": len(data)}


@app.post("/api/prepare")
def api_prepare(req: ModeReq):
    if not pipeline.user_input_present():
        raise HTTPException(400, f"{pipeline.USER_INPUT_NAME} not uploaded yet")
    return {"job_id": _start_job(
        lambda append_log: pipeline.run_prepare(req.mode, on_log=append_log),
        streams_log=True,
        exclusive=True,
    )}


@app.post("/api/validate")
def api_validate(req: ValidateReq):
    errors, warnings = config_schema.validate(req.config, req.mode)
    checks = config_schema.describe_checks(req.config, req.mode)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": checks}


def _compute_job(mode: str, cfg: dict):
    config_path = pipeline.write_runtime_config(mode, cfg)
    return pipeline.run_compute(mode, "python", config_path=config_path)


@app.post("/api/run")
def api_run(req: RunReq):
    errors, _ = config_schema.validate(req.config, req.mode)
    errors.extend(pipeline.engine_request_errors(req.mode, "python", req.config))
    if errors:
        raise HTTPException(400, {"errors": errors})
    if not pipeline.stage1_is_current():
        raise HTTPException(400, "input EComTool_Output.xlsx missing or stale; run preparation first")
    return {"job_id": _start_job(
        lambda: _compute_job(req.mode, req.config),
        exclusive=True,
    )}


def _write_report(mode: str, sections: list[str], on_timing=None) -> Path:
    xlsx = pipeline.results_path(mode)
    started = time.perf_counter()
    cached = pipeline.cached_result(mode)
    if cached is not None:
        sheets = cached.bundle.sheets
        load_label = "use cached ResultBundle"
    else:
        pipeline.validate_xlsx(xlsx)
        sheets = report_mod.load(xlsx)
        load_label = "validate/load Results"
    if on_timing:
        on_timing(load_label, time.perf_counter() - started)
    started = time.perf_counter()
    html = report_mod.build_html(sheets, xlsx, sections=sections)
    if on_timing:
        on_timing("render HTML/charts", time.perf_counter() - started)
    out = report_path(mode)
    started = time.perf_counter()
    out.write_text(html, encoding="utf-8")
    pipeline.publish_report_manifest(
        mode, out, result_key=cached.key if cached is not None else None,
        stage1_identity=(cached.stage1_identity if cached is not None else None),
    )
    if on_timing:
        on_timing("write/manifest", time.perf_counter() - started)
    return out


def _queue_exports(mode: str) -> tuple[dict, dict, str]:
    """Queue Stage 1 and Stage 2 Output XLSX exports; return both states and warnings."""
    warnings = []
    try:
        stage1 = pipeline.queue_stage1_export()
    except RuntimeError as exc:
        stage1 = pipeline.stage1_export_state()
        warnings.append(f"[WARN] Stage 1 Output XLSX not queued: {exc}\n")
    try:
        results = pipeline.queue_results_export(mode)
    except RuntimeError as exc:
        results = pipeline.result_export_state(mode)
        warnings.append(f"[WARN] Stage 2 Output XLSX not queued: {exc}\n")
    return stage1, results, "".join(warnings)


def _analysis_job(
    mode: str, cfg: dict, sections: list[str], append_log, set_stage=None, *,
    force_stage1: bool = False,
):
    total_start = time.perf_counter()
    chunks: list[str] = []
    run_id = pipeline.new_run_id("ui-e2e", mode)
    archive_log, archive_json = pipeline.performance_diagnostic_paths(run_id)
    timings = {
        "run_id": run_id,
        "workflow": "ui-e2e",
        "mode": mode.lower(),
        "engine": "python",
        "cold_stage1": force_stage1,
        "results_export_requested": True,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "stage1_seconds": None,
        "stage2_seconds": None,
        "report_seconds": None,
        "total_seconds": None,
        "system": pipeline.system_metadata(),
        "input": pipeline.file_metadata(pipeline.user_input_path(), sha256=True),
        "diagnostics": {"log": str(archive_log), "json": str(archive_json)},
    }

    def emit(text: str) -> None:
        chunks.append(text)
        append_log(text)

    def update_stage(stage: str) -> None:
        if set_stage is not None:
            set_stage(stage)

    def record(label: str, key: str, started: float) -> None:
        elapsed = round(time.perf_counter() - started, 3)
        timings[key] = elapsed
        emit(f"[TIMING] {label}: {elapsed:.3f} s\n")

    def finish(rc: int, status: str, artifacts: dict | None = None):
        timings["status"] = status
        timings["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        timings["total_seconds"] = round(time.perf_counter() - total_start, 3)
        timings["stage1_cache_hit"] = any("reused cached" in part for part in chunks)
        timings["artifacts"] = {
            "stage1": (pipeline.file_metadata(pipeline.stage1_output_path())
                       if pipeline.stage1_is_current() else None),
            "results": (pipeline.file_metadata(pipeline.results_path(mode))
                        if pipeline.results_are_current(mode) else None),
            "report": pipeline.file_metadata(report_path(mode)),
        }
        emit(f"[TIMING] Total: {timings['total_seconds']:.3f} s\n")
        log_text = "".join(chunks)
        timings["details_seconds"] = pipeline.parse_perf_timings(log_text)
        log_path, metrics_path = pipeline.write_run_diagnostics(
            mode, log_text, timings, run_id=run_id
        )
        result = dict(artifacts or {})
        result.update({
            "log": f"/outputs/runtime/{log_path.name}",
            "timing": f"/outputs/runtime/{metrics_path.name}",
            "performance_log": f"/outputs/runtime/performance/{archive_log.name}",
            "performance_timing": f"/outputs/runtime/performance/{archive_json.name}",
            "timings": timings,
        })
        return rc, log_text, result

    emit(f"[PERF] Run ID: {run_id}\n")
    emit("[PERF] Workflow: UI end-to-end | engine=python\n")
    emit(
        f"[PERF] Machine: {timings['system']['computer_name']} | "
        f"CPU threads={timings['system']['logical_cpu_count']} | "
        f"Python={timings['system']['python_version']}\n"
    )

    try:
        update_stage("stage1")
        stage_start = time.perf_counter()
        rc, prepare_log = pipeline.run_prepare(
            mode, on_log=append_log, force=force_stage1,
            memory_first=True, keep_export_frame=True,
        )
        chunks.append(prepare_log)
        record("Stage-1", "stage1_seconds", stage_start)
        if rc != 0:
            return finish(rc, "failed")

        update_stage("stage2")
        emit("\nStarting Python Stage-2 compute...\n")
        stage_start = time.perf_counter()
        config_path = pipeline.write_runtime_config(mode, cfg)
        rc, compute_log = pipeline.run_compute(
            mode, "python", config_path=config_path,
            export_results=False, async_export=False,
        )
        compute_text = compute_log + ("\n" if not compute_log.endswith("\n") else "")
        emit(compute_text)
        record("Python Stage-2", "stage2_seconds", stage_start)
        if rc != 0:
            return finish(rc, "failed")

        update_stage("report")
        emit("Generating report...\n")
        stage_start = time.perf_counter()
        _write_report(
            mode, sections,
            on_timing=lambda label, elapsed: emit(
                f"[PERF] Report {label}: {elapsed:.3f} s\n"
            ),
        )
        record("Report", "report_seconds", stage_start)
        emit("Stage 1 and Stage 2 Output XLSX: exporting in background\n")
        emit("Analysis complete.\n")
        artifacts = {
            "stage1": (f"/outputs/runtime/{pipeline.STAGE1_OUTPUT_NAME}"
                       if pipeline.stage1_is_current() else None),
            "results": (f"/outputs/runtime/{pipeline.results_path(mode).name}"
                        if pipeline.results_are_current(mode) else None),
            "report": f"/outputs/{report_path(mode).name}",
        }
        rc, log_text, result = finish(0, "success", artifacts)
        # Queue exports only after finish(): its file checks competed with the
        # export thread for the GIL and delayed the report by 10-13 s.
        result["stage1_export"], result["results_export"], warnings = _queue_exports(mode)
        return rc, log_text + warnings, result
    except Exception:  # noqa: BLE001
        emit("Analysis failed:\n" + traceback.format_exc())
        return finish(-1, "failed")


@app.post("/api/analyze")
def api_analyze(req: AnalyzeReq):
    errors, _ = config_schema.validate(req.config, req.mode)
    errors.extend(pipeline.engine_request_errors(req.mode, "python", req.config))
    if errors:
        raise HTTPException(400, {"errors": errors})
    if not pipeline.user_input_present():
        raise HTTPException(400, f"{pipeline.USER_INPUT_NAME} not uploaded yet")
    return {"job_id": _start_job(
        lambda append_log, set_stage: _analysis_job(
            req.mode, req.config, req.sections, append_log, set_stage,
            force_stage1=req.cold_stage1,
        ),
        streams_log=True,
        tracks_stage=True,
        exclusive=True,
    )}


@app.get("/api/status/{job_id}")
def api_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {
        "status": job["status"],
        "stage": job["stage"],
        "rc": job["rc"],
        "log_tail": job["log"][-4000:],
        "result": job.get("result"),
        "started_at_utc": job["started_at_utc"],
        "finished_at_utc": job["finished_at_utc"],
        "elapsed_seconds": job["elapsed_seconds"],
    }


@app.post("/api/report")
@_exclusive_pipeline_call
def api_report(req: ReportReq):
    if not pipeline.calculation_is_current(req.mode):
        raise HTTPException(400, "no calculation result yet — run compute first")
    _write_report(req.mode, req.sections)
    return {"ok": True, "url": f"/outputs/{report_path(req.mode).name}"}


@app.post("/api/export-results")
def api_export_results(req: ModeReq):
    try:
        return pipeline.queue_results_export(req.mode)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/export-results/{mode}")
def api_export_results_status(mode: str):
    state = pipeline.result_export_state(mode)
    if state["status"] == "ready":
        state["url"] = f"/outputs/runtime/{pipeline.results_path(mode).name}"
    return state


@app.get("/api/export-stage1")
def api_export_stage1_status():
    state = pipeline.stage1_export_state()
    if state["status"] == "ready":
        state["url"] = f"/outputs/runtime/{pipeline.STAGE1_OUTPUT_NAME}"
    return state


@app.post("/api/export-stage1")
def api_export_stage1():
    try:
        return pipeline.queue_stage1_export()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)), name="outputs")
