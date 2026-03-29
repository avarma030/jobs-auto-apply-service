from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openpyxl", types.SimpleNamespace())

import src.api.routers.jobs as jobs_router
from src.database.models import JobRecord


def make_job_record(**overrides) -> JobRecord:
    base = dict(
        id=42,
        user_id=1,
        title="Senior ML Engineer",
        company="Example Corp",
        source_board="linkedin",
        url="https://www.linkedin.com/jobs/view/42/",
        application_status="applied",
        tailored_resume_path="data/uploads/1/tailored/42/resume.pdf",
        cover_letter_path="data/uploads/1/tailored/42/cover_letter.md",
    )
    base.update(overrides)
    return JobRecord(**base)


def test_resolve_job_artifact_path_accepts_files_under_upload_root(tmp_path, monkeypatch):
    upload_root = tmp_path / "data" / "uploads"
    artifact = upload_root / "1" / "tailored" / "42" / "resume.pdf"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(jobs_router, "_ARTIFACT_ROOT", upload_root.resolve())
    record = make_job_record(tailored_resume_path=str(artifact))

    resolved = jobs_router._resolve_job_artifact_path(record, "resume")

    assert resolved == artifact.resolve()


def test_resolve_job_artifact_path_rejects_paths_outside_upload_root(tmp_path, monkeypatch):
    upload_root = tmp_path / "data" / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "secrets" / "resume.pdf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"nope")

    monkeypatch.setattr(jobs_router, "_ARTIFACT_ROOT", upload_root.resolve())
    record = make_job_record(tailored_resume_path=str(outside))

    assert jobs_router._resolve_job_artifact_path(record, "resume") is None


def test_artifact_filename_is_human_readable():
    record = make_job_record(title="ML Engineer (Python)", company="ACME & Sons")

    filename = jobs_router._artifact_filename(record, "cover-letter", ".md")

    assert filename == "acme-sons_ml-engineer-python_cover-letter.md"
