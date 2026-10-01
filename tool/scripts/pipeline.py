"""Orchestration for the EComTool pipeline: config -> compute -> Results.

Thin, UI-agnostic layer shared by the CLI and FastAPI admin. The Python engine
computes SC and ARB. A development checkout may add the MATLAB reference engine
through the unshipped ``matlab_engine`` module.
"""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
import importlib.util
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

REPO = Path(__file__).resolve().parents[2]
STAGE1_DIR = REPO / "inputs"


def _resolve_settings_path(repo: Path) -> Path:
    """Use local settings, or parent repo settings for a local Publish checkout."""
    local = repo / "config" / "settings.json"
    parent = repo.parent / "config" / "settings.json"
    return parent if not local.is_file() and parent.is_file() else local


RUNTIME_RESULTS_DIR = REPO / "outputs" / "runtime"
UPLOAD_DIR = RUNTIME_RESULTS_DIR / "uploads"
RUNTIME_CONFIG_DIR = RUNTIME_RESULTS_DIR / "configs"
PERFORMANCE_DIR_NAME = "performance"
PARITY_DIR_NAME = "parity"

STAGE1_SCRIPT = "ecomtool_stage_1.py"
STAGE1_SCRIPT_PATH = Path(__file__).resolve().with_name(STAGE1_SCRIPT)
USER_INPUT_NAME = "EComTool_User_Input.xlsx"
STAGE1_OUTPUT_NAME = "EComTool_Output.xlsx"
STAGE1_CACHE_META_NAME = "EComTool_Output.cache.json"
RESULT_MANIFEST_SUFFIX = ".result-manifest.json"
CALCULATION_MANIFEST_SUFFIX = ".calculation-manifest.json"
REPORT_MANIFEST_SUFFIX = ".report-manifest.json"
STAGE1_CACHE_VERSION = 3
STAGE1_DEPENDENCY_DIRS = ("Database", "User Own Input")
RESULT_CACHE_SCHEMA_VERSION = 1
RESULT_ENGINE_VERSION = "python-stage2-v3"
RESULT_CACHE_MAX_ENTRIES = 2
CACHE_RETENTION_DEFAULT = 5

_UPLOAD_CACHE_RE = re.compile(r"^EComTool_User_Input\.[0-9a-f]{32}\.xlsx$")
_CONFIG_CACHE_RE = re.compile(r"^(sc|arb)\.[0-9a-f]{32}\.json$")
_UI_PERFORMANCE_CACHE_RE = re.compile(
    r"^\d{8}T\d{6}\.\d{6}Z-ui-e2e-(sc|arb)-[0-9a-f]{8}\.(log|json)$"
)
_UI_PARITY_CACHE_RE = re.compile(
    r"^\d{8}T\d{6}\.\d{6}Z-ui-e2e-(sc|arb)-[0-9a-f]{8}-"
    r"(python|matlab)-(sc|arb)\.(xlsx|json)$"
)

SETTINGS_PATH = _resolve_settings_path(REPO)


@dataclass(frozen=True)
class CachedResult:
    """One ready, immutable in-memory Stage-2 result."""

    key: str
    mode: str
    bundle: Any
    input_identity: dict
    stage1_identity: dict
    config_hash: str
    price_identity: dict
    created_at_utc: str


_RESULT_CACHE_LOCK = threading.RLock()
_RESULT_CACHE: OrderedDict[str, CachedResult] = OrderedDict()
_LATEST_RESULT_KEY: dict[str, str] = {}
_RESULT_EXPORT_STATES: dict[str, dict] = {}
_XLSX_EXPORT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xlsx-export")
_PREPARED_DATA_LOCK = threading.RLock()
_PREPARED_DATA: Any | None = None
_STAGE1_EXPORT_STATES: dict[str, dict] = {}


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 10, delay: float = 0.1) -> None:
    """os.replace hardened for Windows: antivirus/indexer may transiently lock the
    freshly written temp file, making the rename fail with PermissionError
    (WinError 5/32). Retry with backoff instead of surfacing a one-off lock.
    """
    import time

    for i in range(attempts):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def _load_settings() -> dict:
    if SETTINGS_PATH.is_file():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def python_exe() -> str:
    """Python interpreter for Stage-1. Precedence: settings.json > PYTHON_EXE env >
    the current interpreter (sys.executable)."""
    s = _load_settings().get("python_exe")
    return s or os.environ.get("PYTHON_EXE") or sys.executable


@dataclass(frozen=True)
class Model:
    mode: str
    results_name: str


MODELS: dict[str, Model] = {
    "arb": Model("arb", "Results_PV_BESS_ARB_20y.xlsx"),
    "sc":  Model("sc",  "Results_PV_BESS_20y.xlsx"),
}


def model(mode: str) -> Model:
    m = MODELS.get(mode.lower())
    if m is None:
        raise ValueError(f"unknown mode {mode!r} (expected 'arb' or 'sc')")
    return m


def default_config_path(mode: str) -> Path:
    return REPO / "tool" / "defaults" / (f"{mode.lower()}_default.json")


def default_config(mode: str, *, current_year: int | None = None) -> dict:
    """Load canonical defaults and resolve clock-dependent values at runtime."""
    cfg = json.loads(default_config_path(mode).read_text(encoding="utf-8"))
    year = date.today().year if current_year is None else int(current_year)
    cfg["simulation_start_year"] = max(2026, year)
    return cfg


def results_path(mode: str) -> Path:
    m = model(mode)
    return RUNTIME_RESULTS_DIR / m.results_name


def price_path(country: str) -> Path:
    """Return country-specific 20-year spot-price forecast."""
    normalized = str(country).strip().casefold()
    if normalized in {"ukraine", "україна", "ua"}:
        filename = "Price_EUR_kWh_UA.xlsx"
    elif normalized in {"latvia", "latvija", "lv"}:
        filename = "Price_EUR_kWh_LV.xlsx"
    else:
        raise ValueError(f"unsupported or missing country for price forecast: {country!r}")

    path = REPO / "price_forecast" / filename
    if not path.is_file():
        raise FileNotFoundError(f"country price forecast is missing: {path}")
    return path


def stage1_output_path() -> Path:
    return RUNTIME_RESULTS_DIR / STAGE1_OUTPUT_NAME


def stage1_cache_path() -> Path:
    return RUNTIME_RESULTS_DIR / STAGE1_CACHE_META_NAME


def run_log_path(mode: str) -> Path:
    model(mode)
    return RUNTIME_RESULTS_DIR / f"analysis_{mode.lower()}.log"


def timing_path(mode: str) -> Path:
    model(mode)
    return RUNTIME_RESULTS_DIR / f"analysis_{mode.lower()}_timing.json"


def result_manifest_path(mode: str) -> Path:
    model(mode)
    return RUNTIME_RESULTS_DIR / f"{mode.lower()}{RESULT_MANIFEST_SUFFIX}"


def calculation_manifest_path(mode: str) -> Path:
    model(mode)
    return RUNTIME_RESULTS_DIR / f"{mode.lower()}{CALCULATION_MANIFEST_SUFFIX}"


def report_manifest_path(mode: str) -> Path:
    model(mode)
    return RUNTIME_RESULTS_DIR / f"{mode.lower()}{REPORT_MANIFEST_SUFFIX}"


def _write_json_atomic(target: Path, value: dict) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        _replace_with_retry(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target


def _artifact_identity(path: Path) -> dict:
    path = Path(path)
    return {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_cache_key(mode: str, config: dict, stage1_path: Path | None,
                      price_xlsx: Path, *,
                      prepared_fingerprint: str | None = None,
                      ) -> tuple[str, dict, dict, str, dict]:
    """Build complete Phase-1 cache identity without depending on XLSX output."""
    normalized = model(mode).mode
    input_identity = _artifact_identity(user_input_path())
    if prepared_fingerprint is None and stage1_path is None:
        raise ValueError("stage1_path or prepared_fingerprint is required")
    stage1_identity = (
        {"kind": "prepared", "fingerprint": prepared_fingerprint}
        if prepared_fingerprint is not None
        else _artifact_identity(Path(stage1_path))
    )
    price_identity = _artifact_identity(price_xlsx)
    config_hash = _canonical_json_hash(config)
    payload = {
        "schema_version": RESULT_CACHE_SCHEMA_VERSION,
        "engine_version": RESULT_ENGINE_VERSION,
        "mode": normalized,
        "input": input_identity,
        "stage1": stage1_identity,
        "config_hash": config_hash,
        "price": price_identity,
    }
    return (
        _canonical_json_hash(payload), input_identity, stage1_identity,
        config_hash, price_identity,
    )


def _publish_calculation_manifest(entry: CachedResult) -> Path:
    """Persist newest compute identity so older XLSX exports stay stale after restart."""
    payload = {
        "version": RESULT_CACHE_SCHEMA_VERSION,
        "mode": entry.mode,
        "created_at_utc": entry.created_at_utc,
        "result_key": entry.key,
        "input": entry.input_identity,
        "stage1": entry.stage1_identity,
        "config_hash": entry.config_hash,
        "price": entry.price_identity,
    }
    return _write_json_atomic(calculation_manifest_path(entry.mode), payload)


def cache_result_bundle(bundle: Any, *, config: dict,
                        stage1_path: Path | None = None,
                        price_xlsx: Path,
                        prepared_fingerprint: str | None = None) -> CachedResult:
    """Atomically publish one complete ResultBundle to bounded L1 RAM cache."""
    key, input_identity, stage1_identity, config_hash, price_identity = _result_cache_key(
        bundle.mode, config, stage1_path, price_xlsx,
        prepared_fingerprint=prepared_fingerprint,
    )
    entry = CachedResult(
        key=key,
        mode=bundle.mode,
        bundle=bundle,
        input_identity=input_identity,
        stage1_identity=stage1_identity,
        config_hash=config_hash,
        price_identity=price_identity,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    _publish_calculation_manifest(entry)
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE[key] = entry
        _RESULT_CACHE.move_to_end(key)
        _LATEST_RESULT_KEY[bundle.mode] = key
        while len(_RESULT_CACHE) > RESULT_CACHE_MAX_ENTRIES:
            expired_key, _ = _RESULT_CACHE.popitem(last=False)
            _RESULT_EXPORT_STATES.pop(expired_key, None)
            for cached_mode, latest_key in tuple(_LATEST_RESULT_KEY.items()):
                if latest_key == expired_key:
                    _LATEST_RESULT_KEY.pop(cached_mode, None)
    return entry


def clear_result_cache() -> None:
    """Drop L1 result references. Running exporters keep their captured snapshot."""
    with _RESULT_CACHE_LOCK:
        _RESULT_CACHE.clear()
        _LATEST_RESULT_KEY.clear()


def cache_prepared_data(data: Any) -> Any:
    """Atomically publish complete PreparedData to L1 RAM."""
    global _PREPARED_DATA
    with _PREPARED_DATA_LOCK:
        _PREPARED_DATA = data
        _STAGE1_EXPORT_STATES.pop(data.fingerprint, None)
    return data


def clear_prepared_data_cache() -> None:
    global _PREPARED_DATA
    with _PREPARED_DATA_LOCK:
        _PREPARED_DATA = None


def cached_prepared_data(fingerprint: str | None = None) -> Any | None:
    """Return PreparedData only when it matches current input/dependencies."""
    with _PREPARED_DATA_LOCK:
        data = _PREPARED_DATA
    if data is None:
        return None
    try:
        expected = fingerprint or _stage1_fingerprint(user_input_path())
    except OSError:
        return None
    return data if data.fingerprint == expected else None


def _set_stage1_export_state(fingerprint: str, status: str,
                             error: str | None = None) -> dict:
    state = {"fingerprint": fingerprint, "status": status, "error": error}
    with _PREPARED_DATA_LOCK:
        _STAGE1_EXPORT_STATES[fingerprint] = state
    return dict(state)


def stage1_export_state() -> dict:
    data = cached_prepared_data()
    fingerprint = data.fingerprint if data is not None else None
    if fingerprint is not None:
        with _PREPARED_DATA_LOCK:
            state = _STAGE1_EXPORT_STATES.get(fingerprint)
        if state is not None:
            return dict(state)
    if stage1_is_current():
        status = "ready"
    elif data is not None and data.legacy_frame is not None:
        status = "available"
    else:
        status = "not_requested"
    return {"fingerprint": fingerprint, "status": status, "error": None}


def _export_prepared_entry(data: Any) -> Path:
    """Export captured Stage-1 table without blocking calculation/report."""
    global _PREPARED_DATA
    from stage_1_core import export_stage1_xlsx

    final_path = stage1_output_path()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = final_path.with_name(
        f".{final_path.stem}.{data.fingerprint[:12]}.{uuid4().hex}.export{final_path.suffix}"
    )
    _set_stage1_export_state(data.fingerprint, "exporting")
    try:
        export_stage1_xlsx(data, staged_path)
        with _PREPARED_DATA_LOCK:
            current = _PREPARED_DATA
            if current is None or current.fingerprint != data.fingerprint:
                raise RuntimeError("Stage-1 export superseded by newer input")
            _replace_with_retry(staged_path, final_path)
            _publish_stage1_cache(data.fingerprint, final_path)
            _PREPARED_DATA = data.compact()
        _set_stage1_export_state(data.fingerprint, "ready")
        return final_path
    except Exception as exc:
        _set_stage1_export_state(
            data.fingerprint, "failed", f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        staged_path.unlink(missing_ok=True)


def queue_stage1_export() -> dict:
    data = cached_prepared_data()
    if data is None:
        raise RuntimeError("no current PreparedData available")
    state = stage1_export_state()
    if state["status"] in {"queued", "exporting", "ready"}:
        return state
    if data.legacy_frame is None:
        raise RuntimeError("PreparedData export snapshot was not retained")
    state = _set_stage1_export_state(data.fingerprint, "queued")
    # Return the state set before submit: re-checking currency here would
    # compete with the just-started export thread for the GIL.
    _XLSX_EXPORT_EXECUTOR.submit(_export_prepared_entry, data)
    return state


def export_stage1_now() -> Path:
    data = cached_prepared_data()
    if data is None or data.legacy_frame is None:
        raise RuntimeError("no retained PreparedData export snapshot available")
    return _export_prepared_entry(data)


def _stage1_identity_is_current(identity: dict) -> bool:
    if identity.get("kind") == "prepared":
        fingerprint = identity.get("fingerprint")
        if not isinstance(fingerprint, str):
            return False
        if cached_prepared_data(fingerprint) is not None:
            return True
        try:
            return fingerprint == _stage1_fingerprint(user_input_path())
        except OSError:
            return False
    try:
        return identity == _artifact_identity(stage1_output_path())
    except OSError:
        return False


def cached_result(mode: str) -> CachedResult | None:
    """Return newest ready bundle only while input and Stage-1 snapshot still match."""
    normalized = model(mode).mode
    with _RESULT_CACHE_LOCK:
        key = _LATEST_RESULT_KEY.get(normalized)
        entry = _RESULT_CACHE.get(key) if key else None
    if entry is None:
        return None
    try:
        current = (
            entry.input_identity == _artifact_identity(user_input_path())
            and _stage1_identity_is_current(entry.stage1_identity)
        )
    except OSError:
        current = False
    if current:
        with _RESULT_CACHE_LOCK:
            if key in _RESULT_CACHE:
                _RESULT_CACHE.move_to_end(key)
        return entry
    return None


def calculation_is_current(mode: str) -> bool:
    """True when reportable tables exist in RAM or current legacy XLSX."""
    return cached_result(mode) is not None or results_are_current(mode)


def publish_result_manifest(mode: str, *, stage1_path: Path | None = None,
                            stage1_identity: dict | None = None,
                            result_key: str | None = None) -> Path:
    """Bind Results workbook to current uploaded input and Stage-1 output."""
    payload = {
        "mode": mode.lower(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": _artifact_identity(user_input_path()),
        "stage1": (stage1_identity if stage1_identity is not None
                   else _artifact_identity(stage1_path or stage1_output_path())),
        "results": _artifact_identity(results_path(mode)),
    }
    if result_key is not None:
        payload["result_key"] = result_key
    return _write_json_atomic(result_manifest_path(mode), payload)


def publish_report_manifest(mode: str, path: Path, *, result_key: str | None = None,
                            stage1_identity: dict | None = None) -> Path:
    """Bind HTML report to its in-memory bundle or legacy Results workbook."""
    payload = {
        "mode": mode.lower(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "report": _artifact_identity(path),
    }
    if result_key is None:
        payload["results"] = _artifact_identity(results_path(mode))
    else:
        payload.update({
            "result_key": result_key,
            "input": _artifact_identity(user_input_path()),
            "stage1": (stage1_identity if stage1_identity is not None
                       else _artifact_identity(stage1_output_path())),
        })
    return _write_json_atomic(report_manifest_path(mode), payload)


def _manifest_matches(path: Path, expected: dict[str, Path]) -> bool:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return all(manifest[name] == _artifact_identity(target) for name, target in expected.items())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _current_calculation_manifest(mode: str) -> dict | None:
    try:
        manifest = json.loads(calculation_manifest_path(mode).read_text(encoding="utf-8"))
        if (
            manifest.get("version") == RESULT_CACHE_SCHEMA_VERSION
            and manifest.get("input") == _artifact_identity(user_input_path())
            and isinstance(manifest.get("stage1"), dict)
            and _stage1_identity_is_current(manifest["stage1"])
            and isinstance(manifest.get("result_key"), str)
        ):
            return manifest
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return None


def results_are_current(mode: str) -> bool:
    """True only when Results belong to newest uploaded input and Stage-1 output."""
    manifest_path = result_manifest_path(mode)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts_match = (
            manifest.get("input") == _artifact_identity(user_input_path())
            and manifest.get("results") == _artifact_identity(results_path(mode))
            and isinstance(manifest.get("stage1"), dict)
            and _stage1_identity_is_current(manifest["stage1"])
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    if not artifacts_match:
        return False
    entry = cached_result(mode)
    calculation = _current_calculation_manifest(mode)
    expected_key = entry.key if entry is not None else (
        calculation.get("result_key") if calculation is not None else None
    )
    return expected_key is None or manifest.get("result_key") == expected_key


def report_is_current(mode: str, path: Path) -> bool:
    """True only when report belongs to current memory result or Results workbook."""
    manifest_path = report_manifest_path(mode)
    entry = cached_result(mode)
    if entry is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return (
                manifest.get("result_key") == entry.key
                and manifest.get("input") == entry.input_identity
                and manifest.get("stage1") == entry.stage1_identity
                and manifest.get("report") == _artifact_identity(path)
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
    calculation = _current_calculation_manifest(mode)
    if calculation is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "result_key" in manifest:
                return (
                    manifest.get("result_key") == calculation["result_key"]
                    and manifest.get("input") == calculation["input"]
                    and manifest.get("stage1") == calculation["stage1"]
                    and manifest.get("report") == _artifact_identity(path)
                )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False
    return results_are_current(mode) and _manifest_matches(manifest_path, {
        "results": results_path(mode), "report": path,
    })


def _set_result_export_state(entry: CachedResult, status: str,
                             error: str | None = None) -> dict:
    state = {
        "mode": entry.mode,
        "result_key": entry.key,
        "status": status,
        "error": error,
    }
    with _RESULT_CACHE_LOCK:
        _RESULT_EXPORT_STATES[entry.key] = state
    return dict(state)


def result_export_state(mode: str) -> dict:
    """Return export state for current result snapshot."""
    normalized = model(mode).mode
    entry = cached_result(normalized)
    if entry is None:
        status = "ready" if results_are_current(normalized) else "not_requested"
        return {"mode": normalized, "result_key": None, "status": status, "error": None}
    with _RESULT_CACHE_LOCK:
        state = _RESULT_EXPORT_STATES.get(entry.key)
    if state is not None:
        return dict(state)
    status = "ready" if results_are_current(normalized) else "not_requested"
    return {"mode": normalized, "result_key": entry.key, "status": status, "error": None}


def _export_cached_entry(entry: CachedResult) -> Path:
    """Export captured snapshot, publishing only if it remains newest."""
    from ecom_port.run import export_results_xlsx

    final_path = results_path(entry.mode)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = final_path.with_name(
        f".{final_path.stem}.{entry.key[:12]}.{uuid4().hex}.export{final_path.suffix}"
    )
    _set_result_export_state(entry, "exporting")
    try:
        export_results_xlsx(entry.bundle, staged_path)
        with _RESULT_CACHE_LOCK:
            if _LATEST_RESULT_KEY.get(entry.mode) != entry.key:
                raise RuntimeError("result export superseded by newer calculation")
            _replace_with_retry(staged_path, final_path)
            publish_result_manifest(
                entry.mode, stage1_identity=entry.stage1_identity,
                result_key=entry.key,
            )
        _set_result_export_state(entry, "ready")
        return final_path
    except Exception as exc:
        _set_result_export_state(entry, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        staged_path.unlink(missing_ok=True)


def export_results_now(mode: str) -> Path:
    """Synchronously export current RAM result for CLI compatibility."""
    entry = cached_result(mode)
    if entry is None:
        raise RuntimeError(f"no cached {mode.upper()} calculation available")
    return _export_cached_entry(entry)


def _matching_cache_files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    """Return regular files with exact app-owned names; ignore temp/unknown files."""
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    return [
        path for path in entries
        if path.is_file() and not path.name.startswith(".") and pattern.fullmatch(path.name)
    ]


def _cache_file_groups(paths: list[Path], *, paired: bool) -> list[list[Path]]:
    """Group one-file cache entries or same-stem archive pairs, newest first."""
    if paired:
        by_stem: dict[str, list[Path]] = {}
        for path in paths:
            by_stem.setdefault(path.stem, []).append(path)
        groups = list(by_stem.values())
    else:
        groups = [[path] for path in paths]

    def modified(group: list[Path]) -> int:
        values = []
        for path in group:
            try:
                values.append(path.stat().st_mtime_ns)
            except FileNotFoundError:
                pass
        return max(values, default=0)

    return sorted(groups, key=modified, reverse=True)


def _runtime_cache_categories() -> dict[str, list[list[Path]]]:
    """Return retention groups for known runtime artifacts only."""
    uploads = _matching_cache_files(UPLOAD_DIR, _UPLOAD_CACHE_RE)
    configs = _matching_cache_files(RUNTIME_CONFIG_DIR, _CONFIG_CACHE_RE)
    performance = _matching_cache_files(
        RUNTIME_RESULTS_DIR / PERFORMANCE_DIR_NAME,
        _UI_PERFORMANCE_CACHE_RE,
    )
    parity = _matching_cache_files(
        RUNTIME_RESULTS_DIR / PARITY_DIR_NAME,
        _UI_PARITY_CACHE_RE,
    )
    upload_groups = _cache_file_groups(uploads, paired=False)
    try:
        current_upload = user_input_path().resolve()
    except OSError:
        current_upload = None
    if current_upload is not None:
        for index, group in enumerate(upload_groups):
            if any(path.resolve() == current_upload for path in group):
                upload_groups.insert(0, upload_groups.pop(index))
                break
    return {
        "uploads": upload_groups,
        "configs_sc": _cache_file_groups(
            [path for path in configs if path.name.startswith("sc.")], paired=False,
        ),
        "configs_arb": _cache_file_groups(
            [path for path in configs if path.name.startswith("arb.")], paired=False,
        ),
        "performance": _cache_file_groups(performance, paired=True),
        "parity": _cache_file_groups(parity, paired=True),
    }


def _unlink_cache_file(path: Path) -> None:
    """Small seam for Windows-lock handling and unit tests."""
    path.unlink()


def cleanup_runtime_cache(*, keep: int = CACHE_RETENTION_DEFAULT,
                          dry_run: bool = True) -> dict:
    """Preview or remove old app-owned runtime artifacts.

    Retention is applied per category. Unknown files, hidden temp files, CLI
    diagnostics and comparison artifacts are outside this operation.
    """
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise ValueError("keep must be an integer >= 1")

    categories = _runtime_cache_categories()
    category_summary: dict[str, dict] = {}
    candidates: list[tuple[str, Path, int]] = []
    for name, groups in categories.items():
        selected_groups = groups[keep:]
        selected_files = [path for group in selected_groups for path in group]
        selected_bytes = 0
        existing_files: list[tuple[Path, int]] = []
        for path in selected_files:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            selected_bytes += size
            existing_files.append((path, size))
            candidates.append((name, path, size))
        category_summary[name] = {
            "found_runs": len(groups),
            "retained_runs": min(len(groups), keep),
            "selected_runs": len(selected_groups),
            "selected_files": len(existing_files),
            "selected_bytes": selected_bytes,
            "deleted_files": 0,
            "deleted_bytes": 0,
        }

    result = {
        "dry_run": bool(dry_run),
        "keep": keep,
        "selected_files": len(candidates),
        "selected_bytes": sum(size for _, _, size in candidates),
        "deleted_files": 0,
        "deleted_bytes": 0,
        "skipped": [],
        "categories": category_summary,
    }
    if dry_run:
        return result

    for category, path, size in candidates:
        try:
            _unlink_cache_file(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result["skipped"].append({
                "path": str(path.relative_to(REPO) if path.is_relative_to(REPO) else path),
                "error": str(exc),
            })
            continue
        result["deleted_files"] += 1
        result["deleted_bytes"] += size
        category_summary[category]["deleted_files"] += 1
        category_summary[category]["deleted_bytes"] += size
    return result


def queue_results_export(mode: str) -> dict:
    """Queue optional XLSX export on dedicated single-worker executor."""
    entry = cached_result(mode)
    if entry is None:
        raise RuntimeError(f"no cached {mode.upper()} calculation available")
    state = result_export_state(mode)
    if state["status"] in {"queued", "exporting", "ready"}:
        return state
    state = _set_result_export_state(entry, "queued")
    _XLSX_EXPORT_EXECUTOR.submit(_export_cached_entry, entry)
    return state


def new_run_id(workflow: str, mode: str, label: str | None = None) -> str:
    """Return filesystem-safe ID for one CLI or UI performance run."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    pieces = [workflow, mode, label or ""]
    tag = "-".join(filter(None, (re.sub(r"[^a-zA-Z0-9_.-]+", "-", p).strip("-") for p in pieces)))
    return f"{stamp}-{tag}-{uuid4().hex[:8]}"


def performance_diagnostic_paths(run_id: str) -> tuple[Path, Path]:
    """Return archived log/JSON paths for a generated run ID."""
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", run_id):
        raise ValueError(f"unsafe performance run id: {run_id!r}")
    directory = RUNTIME_RESULTS_DIR / PERFORMANCE_DIR_NAME
    return directory / f"{run_id}.log", directory / f"{run_id}.json"


def system_metadata() -> dict:
    """Collect dependency and machine context needed to compare performance runs."""
    packages = {}
    for name in ("numpy", "scipy", "pandas", "openpyxl", "lxml", "fastapi", "uvicorn"):
        try:
            packages[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
        "computer_name": os.environ.get("COMPUTERNAME", platform.node()),
        "logical_cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "packages": packages,
    }


def parse_perf_timings(log_text: str) -> dict[str, float]:
    """Extract detailed `[PERF] label: seconds` lines into timing JSON."""
    result = {}
    for match in re.finditer(
        r"^\[PERF\]\s+(.+?):\s+([0-9]+(?:\.[0-9]+)?)\s+s\s*$",
        log_text,
        flags=re.MULTILINE,
    ):
        result[match.group(1)] = float(match.group(2))
    return result


def file_metadata(path: Path, *, sha256: bool = False) -> dict:
    """Describe runtime artifact without failing when it does not exist."""
    path = Path(path)
    result = {"path": str(path.resolve()), "exists": path.is_file()}
    if result["exists"]:
        stat = path.stat()
        result.update({"bytes": stat.st_size, "modified_ns": stat.st_mtime_ns})
        if sha256:
            result["sha256"] = _sha256_file(path)
    return result


def write_run_diagnostics(
    mode: str, log_text: str, timings: dict, *, run_id: str | None = None
) -> tuple[Path, Path]:
    """Atomically publish latest diagnostics and optional immutable run archive."""
    log_path = run_log_path(mode)
    metrics_path = timing_path(mode)
    RUNTIME_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payloads = [
        (log_path, log_text),
        (metrics_path, json.dumps(timings, indent=2) + "\n"),
    ]
    if run_id is not None:
        archive_log, archive_metrics = performance_diagnostic_paths(run_id)
        archive_log.parent.mkdir(parents=True, exist_ok=True)
        payloads.extend([
            (archive_log, log_text),
            (archive_metrics, json.dumps(timings, indent=2) + "\n"),
        ])
    for target, payload in payloads:
        temp_path = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            _replace_with_retry(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
    return log_path, metrics_path


def read_run_timings(mode: str) -> dict | None:
    """Read latest timing metrics, returning None for missing/corrupt runtime state."""
    path = timing_path(mode)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def input_present(mode: str) -> bool:
    model(mode)
    return stage1_output_path().is_file()


def user_input_path() -> Path:
    """Return newest browser upload, falling back to tracked local template."""
    uploads = sorted(
        UPLOAD_DIR.glob("EComTool_User_Input.*.xlsx"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    ) if UPLOAD_DIR.is_dir() else []
    return uploads[0] if uploads else STAGE1_DIR / USER_INPUT_NAME


def user_input_present() -> bool:
    return user_input_path().is_file()


def save_user_input(data: bytes) -> Path:
    """Validate upload and store immutable runtime copy without replacing tracked input."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid4().hex
    path = UPLOAD_DIR / f"EComTool_User_Input.{upload_id}.xlsx"
    temp_path = UPLOAD_DIR / f".EComTool_User_Input.{upload_id}.tmp.xlsx"
    try:
        temp_path.write_bytes(data)
        validate_stage1_input(temp_path)
        _replace_with_retry(temp_path, path)
        clear_prepared_data_cache()
        clear_result_cache()
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def validate_xlsx(path: Path) -> None:
    """Reject missing, incomplete, or non-XLSX files before pandas/openpyxl reads them."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"XLSX file is missing: {path}")
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise ValueError(f"File is not a valid XLSX workbook: {path.name}")
    except BadZipFile as exc:
        raise ValueError(f"File is incomplete or not a valid XLSX workbook: {path.name}") from exc


def validate_stage1_input(path: Path) -> None:
    """Require Stage-1 user-input template, not a Results or Stage-1 output workbook."""
    import warnings

    from openpyxl import load_workbook

    validate_xlsx(path)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Data Validation extension is not supported.*")
        workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        required_sheets = {"Input", "Geography", "Consumers", "Profiles", "Tariffs"}
        if not required_sheets.issubset(workbook.sheetnames):
            raise ValueError("Expected EComTool_User_Input.xlsx template (Input sheet is missing)")
        sheet = workbook["Input"]
        expected_labels = {
            "A10": "Existing PV Installed Power, kW",
            "A11": "Projected PV Installed Power, kW",
            "A12": "PV Profile",
            "A28": "Existing BESS Installed Capacity, kWh",
            "A29": "Projected BESS Installed Capacity, kWh",
            "A96": "Planning Horizon, years",
        }
        if any(sheet[cell].value != label for cell, label in expected_labels.items()):
            raise ValueError("Expected EComTool_User_Input.xlsx template; selected workbook has wrong layout")
        if sheet["A4"].value not in {"Ukraine", "Latvia"}:
            raise ValueError("Input!A4 must contain Ukraine or Latvia")
        if str(sheet["C4"].value).strip().lower() not in {"urban", "suburban", "rural"}:
            raise ValueError("Input!C4 Area Type must be Urban, Suburban, or Rural")
    finally:
        workbook.close()


_DIGEST_LOCK = threading.Lock()
_FILE_DIGESTS: dict[str, tuple[int, int, str]] = {}
_FILE_DIGEST_MAX_ENTRIES = 512
_STAGE1_FINGERPRINT: dict[str, Any] = {}
# Files modified this recently are always re-read: one filesystem timestamp tick
# can hide a same-size rewrite (same guard as git's "racy" index entries).
_DIGEST_TRUST_AGE_NS = 2_000_000_000


def _stable_signature(path: Path) -> tuple[int, int] | None:
    """Return (size, mtime_ns) when old enough to trust as a content-change signal."""
    stat = path.stat()
    if time.time_ns() - stat.st_mtime_ns < _DIGEST_TRUST_AGE_NS:
        return None
    return stat.st_size, stat.st_mtime_ns


def _sha256_file(path: Path) -> str:
    """SHA-256 of file content, reused while size and mtime stay unchanged."""
    path = Path(path)
    key = os.path.abspath(path)
    signature = _stable_signature(path)
    if signature is not None:
        with _DIGEST_LOCK:
            cached = _FILE_DIGESTS.get(key)
        if cached is not None and cached[:2] == signature:
            return cached[2]
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if signature is not None and _stable_signature(path) == signature:
        with _DIGEST_LOCK:
            if len(_FILE_DIGESTS) >= _FILE_DIGEST_MAX_ENTRIES:
                _FILE_DIGESTS.clear()
            _FILE_DIGESTS[key] = (*signature, value)
    return value


def _stage1_dependency_files() -> list[Path]:
    files = [STAGE1_SCRIPT_PATH, STAGE1_SCRIPT_PATH.with_name("tariff_io.py"), default_config_path("sc")]
    for directory_name in STAGE1_DEPENDENCY_DIRS:
        directory = STAGE1_DIR / directory_name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(REPO).as_posix())


def _stage1_fingerprint(input_path: Path) -> str:
    """Fingerprint Stage-1 input plus code/profile data that affect its output."""
    inputs = [("input.xlsx", Path(input_path)), *(
        (path.relative_to(REPO).as_posix(), path)
        for path in _stage1_dependency_files()
    )]
    signatures = [_stable_signature(path) for _, path in inputs]
    cache_key = None
    if all(signature is not None for signature in signatures):
        cache_key = (STAGE1_CACHE_VERSION, os.path.abspath(inputs[0][1]),
                     tuple(zip((name for name, _ in inputs), signatures)))
        with _DIGEST_LOCK:
            if _STAGE1_FINGERPRINT.get("key") == cache_key:
                return _STAGE1_FINGERPRINT["value"]

    digest = hashlib.sha256()
    digest.update(f"stage1-cache-v{STAGE1_CACHE_VERSION}\0".encode("ascii"))
    for relative_name, path in inputs:
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    value = digest.hexdigest()
    if cache_key is not None:
        with _DIGEST_LOCK:
            _STAGE1_FINGERPRINT.update(key=cache_key, value=value)
    return value


def _stage1_cache_is_current(fingerprint: str) -> bool:
    output_path = stage1_output_path()
    metadata_path = stage1_cache_path()
    try:
        validate_xlsx(output_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return (
            metadata.get("version") == STAGE1_CACHE_VERSION
            and metadata.get("fingerprint") == fingerprint
            and metadata.get("output_sha256") == _sha256_file(output_path)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def stage1_is_current() -> bool:
    """True when cached Stage-1 output matches newest input and all dependencies."""
    if not user_input_present():
        return False
    return _stage1_cache_is_current(_stage1_fingerprint(user_input_path()))


def _publish_stage1_cache(fingerprint: str, output_path: Path) -> Path:
    """Publish cache metadata after output is safely available."""
    metadata_path = stage1_cache_path()
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": STAGE1_CACHE_VERSION,
        "fingerprint": fingerprint,
        "output_sha256": _sha256_file(output_path),
    }
    temp_path = metadata_path.with_name(f".{metadata_path.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _replace_with_retry(temp_path, metadata_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return metadata_path


def _publish_stage1_output(src: Path, dst: Path) -> None:
    """Publish Stage-1 workbook atomically without changing tariff provenance."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dst.with_name(f".{dst.stem}.{uuid4().hex}.tmp{dst.suffix}")
    try:
        shutil.copy2(src, temp_path)
        validate_xlsx(temp_path)
        _replace_with_retry(temp_path, dst)
    finally:
        temp_path.unlink(missing_ok=True)


def run_prepare(
    mode: str,
    on_log: Callable[[str], None] | None = None,
    *,
    force: bool = False,
    memory_first: bool = False,
    keep_export_frame: bool = False,
) -> tuple[int, str]:
    """Stage-1: prepare data in RAM or publish legacy runtime workbook.

    PYTHONUTF8 avoids a cp1252 crash on a debug print with a '≈' character.
    """
    chunks: list[str] = []

    def emit(text: str) -> None:
        chunks.append(text)
        if on_log is not None:
            on_log(text)

    if not user_input_present():
        return 2, f"{USER_INPUT_NAME} missing in {STAGE1_DIR}"
    import time

    input_path = user_input_path()
    started = time.perf_counter()
    fingerprint = _stage1_fingerprint(input_path)
    emit(f"[PERF] Stage-1 fingerprint: {time.perf_counter() - started:.3f} s\n")
    if memory_first:
        started = time.perf_counter()
        prepared = cached_prepared_data(fingerprint)
        emit(f"[PERF] Stage-1 PreparedData cache check: {time.perf_counter() - started:.3f} s\n")
        if (
            prepared is not None
            and not force
            and (
                not keep_export_frame
                or prepared.legacy_frame is not None
                or _stage1_cache_is_current(fingerprint)
            )
        ):
            emit("[Stage-1] unchanged input; reused cached PreparedData\n")
            return 0, "".join(chunks)
        if force:
            emit("[Stage-1] cold run requested; cache bypassed\n")

        from stage_1_core import prepare_data
        import traceback

        emit(f"Starting Stage-1 memory core: {STAGE1_SCRIPT}\nInput: {input_path.name}\n")
        started = time.perf_counter()
        try:
            prepared = prepare_data(
                input_path, STAGE1_DIR, fingerprint,
                keep_export_frame=keep_export_frame,
                on_log=emit,
            )
            cache_prepared_data(prepared)
            clear_result_cache()
            emit(f"[PERF] Stage-1 prepare/publish RAM: {time.perf_counter() - started:.3f} s\n")
            emit("[Stage-1] PreparedData ready in RAM\n")
            return 0, "".join(chunks)
        except Exception:  # noqa: BLE001
            emit("Stage-1 memory core failed:\n" + traceback.format_exc())
            return 1, "".join(chunks)

    started = time.perf_counter()
    cache_current = _stage1_cache_is_current(fingerprint)
    emit(f"[PERF] Stage-1 cache check: {time.perf_counter() - started:.3f} s\n")
    if cache_current and not force:
        emit(f"[Stage-1] unchanged input; reused cached {STAGE1_OUTPUT_NAME}\n")
        return 0, "".join(chunks)
    if force:
        emit("[Stage-1] cold run requested; cache bypassed\n")

    RUNTIME_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    generated_output = RUNTIME_RESULTS_DIR / f".{STAGE1_OUTPUT_NAME}.{uuid4().hex}.tmp.xlsx"
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "ECOM_STAGE1_INPUT": str(input_path),
        "ECOM_STAGE1_OUTPUT": str(generated_output),
    }
    # Equivalent to the author's manual Stage-1 command, but defaults to the
    # server's own interpreter (sys.executable) so Stage-1 deps (pandas/numpy/
    # openpyxl) are guaranteed present. Overridable via config/settings.json.
    emit(f"Starting Stage-1: {STAGE1_SCRIPT}\nInput: {input_path.name}\n")
    started = time.perf_counter()
    proc = subprocess.Popen(
        [python_exe(), "-u", str(STAGE1_SCRIPT_PATH)],
        cwd=str(STAGE1_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    if proc.stdout is not None:
        for line in proc.stdout:
            emit(line)
    returncode = proc.wait()
    emit(f"[PERF] Stage-1 subprocess: {time.perf_counter() - started:.3f} s\n")

    started = time.perf_counter()
    try:
        if returncode == 0:
            src = generated_output
            dst = stage1_output_path()
            try:
                validate_xlsx(src)
            except ValueError as exc:
                emit(f"\nERROR: {exc}\n")
                return 1, "".join(chunks)
            _publish_stage1_output(src, dst)
            _publish_stage1_cache(fingerprint, dst)
            emit(f"[PERF] Stage-1 validate/publish/cache: {time.perf_counter() - started:.3f} s\n")
            emit(f"\n[Stage-1] published {STAGE1_OUTPUT_NAME} -> outputs/runtime/\n")
        return returncode, "".join(chunks)
    finally:
        generated_output.unlink(missing_ok=True)


def write_runtime_config(mode: str, cfg: dict) -> Path:
    """Store one immutable config snapshot without touching tracked model files."""
    model(mode)
    RUNTIME_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    path = RUNTIME_CONFIG_DIR / f"{mode.lower()}.{run_id}.json"
    temp_path = RUNTIME_CONFIG_DIR / f".{mode.lower()}.{run_id}.tmp.json"
    try:
        temp_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        _replace_with_retry(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _matlab_engine_available() -> bool:
    """True only in a development checkout that carries the unshipped module."""
    return importlib.util.find_spec("matlab_engine") is not None


ENGINES = ("python", "matlab") if _matlab_engine_available() else ("python",)
PYTHON_MODES = ("sc", "arb")   # modes the Python port implements so far


def engine_request_errors(mode: str, engine: str, cfg: dict | None = None) -> list[str]:
    """Validate mode/engine capability before starting a background job."""
    errors: list[str] = []
    normalized_mode = mode.lower()
    normalized_engine = engine.lower()
    if normalized_engine not in ENGINES:
        return [f"engine must be one of {ENGINES}"]
    if normalized_mode not in MODELS:
        return [f"mode must be one of {tuple(MODELS)}"]
    if normalized_engine == "python" and normalized_mode not in PYTHON_MODES:
        errors.append(f"Python engine implements {PYTHON_MODES}; '{mode}' not ported yet")
    if (normalized_engine == "python" and normalized_mode == "sc" and cfg is not None
            and not cfg.get("fast_sc_dispatch", True)):
        errors.append(
            "Python SC engine supports only fast_sc_dispatch=true; "
            "LP-backed SC dispatch is unavailable in user workflow"
        )
    return errors


def run_compute(mode: str, engine: str = "python", config_path: Path | None = None,
                *, export_results: bool = True,
                async_export: bool = False) -> tuple[int, str]:
    """Run Stage-2 compute synchronously. Returns (returncode, combined_log).

    'python' runs the in-repo port. In a development checkout 'matlab' runs the
    reference model through ``matlab_engine``; both write the same Results schema.
    """
    engine = engine.lower()
    if engine == "matlab" and engine in ENGINES:
        from matlab_engine import run_matlab

        return run_matlab(mode, config_path=config_path)
    if engine == "python":
        return _run_python(
            mode, config_path=config_path, export_results=export_results,
            async_export=async_export,
        )
    return 2, f"unknown engine {engine!r} (expected one of {ENGINES})"


def _run_python(mode: str, config_path: Path | None = None, *,
                export_results: bool = True,
                async_export: bool = False) -> tuple[int, str]:
    if mode.lower() not in PYTHON_MODES:
        return 2, f"Python engine implements {PYTHON_MODES}; '{mode}' not ported yet."
    import time
    import traceback

    from ecom_port.frontend import load_config, read_output
    from ecom_port.run import compute_arb, compute_sc

    model(mode)
    out_xlsx = stage1_output_path()
    config = Path(config_path) if config_path else None
    defaults = default_config_path(mode)
    runner = compute_arb if mode.lower() == "arb" else compute_sc
    chunks: list[str] = []

    def record(label: str, elapsed: float) -> None:
        chunks.append(f"[PERF] Stage-2 {label}: {elapsed:.3f} s\n")

    try:
        started = time.perf_counter()
        prepared = cached_prepared_data()
        if prepared is not None:
            inputs = prepared.inputs
            record("use PreparedData RAM", time.perf_counter() - started)
        else:
            validate_xlsx(out_xlsx)
            inputs = read_output(out_xlsx)
            record("validate/read profiles", time.perf_counter() - started)
        price_xlsx = price_path(inputs.country)
        validate_xlsx(price_xlsx)
        config_file = config if config is not None and config.is_file() else None
        resolved_config = load_config(config_file, defaults)
        result = runner(
            out_xlsx, price_xlsx, config_file, defaults,
            inputs=inputs, on_timing=record,
        )
        started = time.perf_counter()
        entry = cache_result_bundle(
            result, config=resolved_config,
            stage1_path=out_xlsx if prepared is None else None,
            price_xlsx=price_xlsx,
            prepared_fingerprint=(prepared.fingerprint if prepared is not None else None),
        )
        record("publish ResultBundle to RAM cache", time.perf_counter() - started)
        if export_results:
            started = time.perf_counter()
            if async_export:
                queue_results_export(mode)
                record("queue Results XLSX export", time.perf_counter() - started)
            else:
                _export_cached_entry(entry)
                record("write Results XLSX", time.perf_counter() - started)
        chunks.append(f"Python {mode.upper()} engine: result tables cached in memory")
        if export_results:
            chunks.append(
                f"; Results XLSX {'queued' if async_export else 'written'}"
            )
        return 0, "".join(chunks)
    except Exception:  # noqa: BLE001
        return 1, "".join(chunks) + "Python engine failed:\n" + traceback.format_exc()
