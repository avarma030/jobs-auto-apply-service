from __future__ import annotations

from pathlib import Path

import fitz  # pymupdf


def parse_resume(resume_path: Path) -> str:
    """Extract all text from a resume PDF, page by page."""
    if not resume_path.exists():
        raise FileNotFoundError(f"Resume not found at {resume_path}")
    doc = fitz.open(str(resume_path))
    pages = [page.get_text() for page in doc]
    doc.close()
    return "\n\n".join(p.strip() for p in pages if p.strip())
