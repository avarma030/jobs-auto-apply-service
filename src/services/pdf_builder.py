from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from reportlab.lib.enums import TA_LEFT


_SECTION_HEADINGS = {"SUMMARY", "EXPERIENCE", "EDUCATION", "SKILLS", "CERTIFICATIONS"}


def build_resume_pdf(resume_text: str, output_path: Path) -> Path:
    """
    Render plain-text resume to a clean PDF using reportlab.
    Detects standard section headings and renders them in bold.
    Returns output_path.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
    )

    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "ResumeNormal",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    heading = ParagraphStyle(
        "ResumeHeading",
        parent=styles["Normal"],
        fontSize=12,
        leading=16,
        fontName="Helvetica-Bold",
        spaceBefore=10,
        spaceAfter=4,
        alignment=TA_LEFT,
    )
    name_style = ParagraphStyle(
        "ResumeName",
        parent=styles["Normal"],
        fontSize=16,
        leading=20,
        fontName="Helvetica-Bold",
        spaceAfter=6,
        alignment=TA_LEFT,
    )

    story = []
    lines = resume_text.splitlines()
    first_non_empty = True

    for line in lines:
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 4))
            continue

        upper = stripped.upper()
        if upper in _SECTION_HEADINGS:
            story.append(Paragraph(stripped.upper(), heading))
        elif first_non_empty:
            # First non-empty line treated as candidate name
            story.append(Paragraph(_escape(stripped), name_style))
            first_non_empty = False
        else:
            first_non_empty = False
            story.append(Paragraph(_escape(stripped), normal))

    doc.build(story)
    return output_path


def _escape(text: str) -> str:
    """Escape XML special chars for reportlab Paragraph."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
