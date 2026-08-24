"""Admin: read-only browse over replay / realtime guard report artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)

ReportKind = Literal["replay", "realtime"]
_KINDS: set[str] = {"replay", "realtime"}


class ReportSummary(BaseModel):
    id: str
    kind: ReportKind
    filename: str
    generated_at: datetime | None = None
    modified_at: datetime
    passed: bool | None = None
    schema_version: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ListReportsResponse(BaseModel):
    reports: list[ReportSummary]


class ReportDetail(BaseModel):
    summary: ReportSummary
    payload: dict[str, Any]


@router.get("/reports", response_model=ListReportsResponse)
async def list_reports(
    request: Request,
    kind: ReportKind | None = Query(default=None),
) -> ListReportsResponse:
    root = _report_root(request)
    kinds = [kind] if kind is not None else sorted(_KINDS)
    reports: list[ReportSummary] = []
    for item_kind in kinds:
        reports.extend(_iter_kind_reports(root, item_kind))
    reports.sort(key=lambda r: r.modified_at, reverse=True)
    return ListReportsResponse(reports=reports)


@router.get("/reports/{kind}/{filename}", response_model=ReportDetail)
async def get_report(
    request: Request,
    kind: ReportKind,
    filename: str,
) -> ReportDetail:
    path = _resolve_report_path(_report_root(request), kind, filename)
    if not path.exists():
        raise HTTPException(404, "report not found")
    payload = _read_json_report(path)
    return ReportDetail(
        summary=_summarize_report(path, kind=kind, payload=payload),
        payload=payload,
    )


def _iter_kind_reports(root: Path, kind: str) -> list[ReportSummary]:
    kind_dir = (root / kind).resolve()
    if not kind_dir.exists():
        return []
    reports: list[ReportSummary] = []
    for path in sorted(kind_dir.glob("*.json")):
        try:
            payload = _read_json_report(path)
        except HTTPException:
            continue
        reports.append(_summarize_report(path, kind=kind, payload=payload))
    return reports


def _report_root(request: Request) -> Path:
    configured = getattr(request.app.state, "reports_dir", None)
    if configured is not None:
        return Path(configured).expanduser().resolve()
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return (settings.runtime.debug_dir / "reports").expanduser().resolve()
    return Path("reports").resolve()


def _resolve_report_path(root: Path, kind: str, filename: str) -> Path:
    if kind not in _KINDS:
        raise HTTPException(404, "unknown report kind")
    if "/" in filename or "\\" in filename or not filename.endswith(".json"):
        raise HTTPException(400, "invalid report filename")
    path = (root / kind / filename).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(400, "invalid report path") from exc
    return path


def _read_json_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"invalid report JSON: {path.name}") from exc
    if not isinstance(data, dict):
        raise HTTPException(422, f"report must be a JSON object: {path.name}")
    return data


def _summarize_report(path: Path, *, kind: str, payload: dict[str, Any]) -> ReportSummary:
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    generated_at = _parse_datetime(payload.get("generated_at"))
    return ReportSummary(
        id=f"{kind}/{path.name}",
        kind=kind,  # type: ignore[arg-type]
        filename=path.name,
        generated_at=generated_at,
        modified_at=modified_at,
        passed=_bool_or_none(payload.get("passed")),
        schema_version=payload.get("schema_version"),
        summary=payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        metrics=payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {},
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
