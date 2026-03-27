from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.models.user_profile import Address, Education, SocialLinks, UserProfile, WorkExperience

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(
    r"(?:(?:\+?\d{1,3}[\s().-]*)?(?:\d[\s().-]*){8,}\d)"
)
URL_RE = re.compile(r"(https?://[^\s|]+)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
YEARS_OF_EXPERIENCE_RE = re.compile(r"\b(\d{1,2})\+?\s+years?\b", re.IGNORECASE)

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

SECTION_ALIASES = {
    "summary": {"summary", "professional summary", "profile", "about", "professional profile"},
    "skills": {
        "skills",
        "technical skills",
        "core skills",
        "core competencies",
        "competencies",
        "technologies",
        "tooling",
        "stack",
    },
    "experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "work history",
    },
    "education": {"education", "academic background", "qualifications"},
    "languages": {"languages", "language skills"},
}

TITLE_HINTS = {
    "engineer",
    "developer",
    "manager",
    "director",
    "lead",
    "architect",
    "analyst",
    "consultant",
    "specialist",
    "administrator",
    "coordinator",
    "designer",
    "scientist",
    "officer",
    "intern",
    "president",
    "founder",
    "owner",
    "principal",
    "product",
    "program",
    "project",
}

COMPANY_HINTS = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "company",
    "technologies",
    "technology",
    "systems",
    "solutions",
    "group",
    "labs",
    "lab",
    "partners",
    "services",
    "holdings",
    "university",
    "college",
}

DEGREE_HINTS = {
    "bachelor",
    "master",
    "mba",
    "phd",
    "doctor",
    "bs",
    "bsc",
    "ba",
    "ms",
    "msc",
    "ma",
    "degree",
    "diploma",
    "certificate",
}

FIELD_HINTS = {
    "computer science",
    "software engineering",
    "information systems",
    "business",
    "economics",
    "marketing",
    "finance",
    "mathematics",
    "data science",
    "artificial intelligence",
    "machine learning",
}

CONTACT_LABELS = {
    "email",
    "phone",
    "mobile",
    "linkedin",
    "github",
    "portfolio",
    "website",
    "location",
}


@dataclass(slots=True)
class ProfileBootstrapResult:
    profile: UserProfile
    output_path: Path
    extracted_fields: list[str] = field(default_factory=list)
    review_notes: list[str] = field(default_factory=list)


def bootstrap_profile_from_resume(
    resume_path: Path | str,
    *,
    output_path: Path | str,
    overwrite: bool = False,
) -> ProfileBootstrapResult:
    resume_path = Path(resume_path)
    output_path = Path(output_path)

    if not resume_path.exists():
        raise FileNotFoundError(f"Resume not found at {resume_path}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Profile already exists at {output_path}. Re-run with --force to overwrite it."
        )

    text = extract_resume_text(resume_path)
    result = build_profile_from_resume_text(text, resume_path=resume_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(
            result.profile.model_dump(mode="json"),
            handle,
            indent=2,
        )
        handle.write("\n")

    result.output_path = output_path
    return result


def build_profile_from_resume_text(text: str, *, resume_path: Path | str) -> ProfileBootstrapResult:
    resume_path = Path(resume_path)
    normalized_text = _normalize_resume_text(text)
    if not normalized_text:
        raise ValueError("The resume appears to be empty. Please provide a resume with extractable text.")

    sections = _split_sections(normalized_text)
    top_lines = [line for line in sections.get("header", []) if line.strip()]

    extracted_fields: list[str] = []
    review_notes: list[str] = []

    email = _extract_email(normalized_text)
    if email:
        extracted_fields.append("email")
    else:
        raise ValueError("Could not find an email address in the resume. Please add one and try again.")

    first_name, last_name = _extract_name(top_lines, email)
    extracted_fields.extend(["first_name", "last_name"])

    phone = _extract_phone(normalized_text)
    if phone:
        extracted_fields.append("phone")
    else:
        review_notes.append("Phone number was not detected. Add it manually if you want forms auto-filled.")

    social_links = _extract_social_links(normalized_text)
    for label in ("linkedin", "github", "portfolio"):
        if getattr(social_links, label):
            extracted_fields.append(f"social_links.{label}")

    address = _extract_address(top_lines)
    if address:
        extracted_fields.append("address")

    headline = _extract_headline(top_lines, first_name, last_name)
    if headline:
        extracted_fields.append("headline")

    summary = _extract_summary(sections, top_lines)
    if summary:
        extracted_fields.append("summary")

    skills = _extract_list_items(sections.get("skills", []))
    if skills:
        extracted_fields.append("skills")
    else:
        review_notes.append("Skills were not confidently detected. Add or refine them manually for better matching.")

    languages = _extract_languages(sections.get("languages", []))
    if languages:
        extracted_fields.append("languages")

    work_experience = _extract_work_experience(sections.get("experience", []))
    if work_experience:
        extracted_fields.append("work_experience")
    else:
        review_notes.append(
            "Work experience could not be reliably structured from the resume. Review that section manually."
        )

    education = _extract_education(sections.get("education", []))
    if education:
        extracted_fields.append("education")

    years_of_experience = _extract_years_of_experience(normalized_text, work_experience)
    if years_of_experience is not None:
        extracted_fields.append("years_of_experience")

    profile = UserProfile(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        address=address,
        headline=headline,
        summary=summary,
        years_of_experience=years_of_experience,
        skills=skills,
        languages=languages or ["English"],
        work_experience=work_experience,
        education=education,
        social_links=social_links,
        resume_path=resume_path,
    )

    return ProfileBootstrapResult(
        profile=profile,
        output_path=Path(),
        extracted_fields=sorted(set(extracted_fields)),
        review_notes=review_notes,
    )


def extract_resume_text(resume_path: Path | str) -> str:
    resume_path = Path(resume_path)
    suffix = resume_path.suffix.lower()

    if suffix == ".txt":
        return resume_path.read_text(encoding="utf-8")

    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError(
                "PDF resume parsing needs PyMuPDF installed. Run `pip install pymupdf` first."
            ) from exc

        with fitz.open(resume_path) as document:
            return "\n\n".join(page.get_text("text") for page in document)

    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError(
                "DOCX resume parsing needs python-docx installed. Run `pip install python-docx` first."
            ) from exc

        document = Document(resume_path)
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    raise ValueError(
        f"Unsupported resume format `{suffix or '<no suffix>'}`. Use PDF, DOCX, or TXT."
    )


def _normalize_resume_text(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\uf0b7", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current_section = "header"

    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if _is_section_heading(line):
            current_section = _canonical_section_name(line)
            sections.setdefault(current_section, [])
            continue
        sections.setdefault(current_section, []).append(line)

    return sections


def _clean_line(line: str) -> str:
    return line.strip().strip("|").strip("-").strip()


def _is_section_heading(line: str) -> bool:
    if not line:
        return False
    normalized = _normalized_heading(line)
    if normalized in {"contact", "contact information"}:
        return False
    return any(normalized in aliases for aliases in SECTION_ALIASES.values())


def _canonical_section_name(line: str) -> str:
    normalized = _normalized_heading(line)
    for section_name, aliases in SECTION_ALIASES.items():
        if normalized in aliases:
            return section_name
    return "header"


def _normalized_heading(line: str) -> str:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", line.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.rstrip(":")


def _extract_email(text: str) -> str | None:
    match = EMAIL_RE.search(text)
    return match.group(0) if match else None


def _extract_phone(text: str) -> str | None:
    match = PHONE_RE.search(text)
    if not match:
        return None
    phone = re.sub(r"\s+", " ", match.group(0)).strip(" .-")
    return phone


def _extract_name(top_lines: list[str], email: str) -> tuple[str, str]:
    for line in top_lines[:6]:
        candidate = line.split("|", 1)[0].strip()
        if _looks_like_name(candidate):
            parts = candidate.split()
            return parts[0], " ".join(parts[1:])

    local_part = email.split("@", 1)[0]
    name_parts = [part for part in re.split(r"[._-]+", local_part) if part]
    if len(name_parts) >= 2:
        return name_parts[0].title(), " ".join(part.title() for part in name_parts[1:])

    raise ValueError(
        "Could not infer a first and last name from the resume header or email address."
    )


def _looks_like_name(value: str) -> bool:
    value = value.strip()
    if not value or "@" in value or "http" in value or any(char.isdigit() for char in value):
        return False
    words = value.split()
    if len(words) < 2 or len(words) > 4:
        return False
    if any(_normalized_heading(word) in CONTACT_LABELS for word in words):
        return False
    return all(re.fullmatch(r"[A-Za-z][A-Za-z'.-]*", word) for word in words)


def _extract_social_links(text: str) -> SocialLinks:
    urls = [match.group(1).rstrip(".,") for match in URL_RE.finditer(text)]

    linkedin = next((url for url in urls if "linkedin.com/" in url.lower()), None)
    github = next((url for url in urls if "github.com/" in url.lower()), None)
    portfolio = next(
        (
            url
            for url in urls
            if url not in {linkedin, github}
            and "linkedin.com/" not in url.lower()
            and "github.com/" not in url.lower()
        ),
        None,
    )

    return SocialLinks(linkedin=linkedin, github=github, portfolio=portfolio)


def _extract_address(top_lines: list[str]) -> Address | None:
    for line in top_lines[:8]:
        if not line or _looks_like_contact_line(line) or "@" in line or "http" in line:
            continue
        if "," not in line:
            continue
        if any(char.isdigit() for char in line):
            continue
        parts = [part.strip() for part in line.split(",") if part.strip()]
        if len(parts) == 2:
            city, second = parts
            if len(second) <= 3 and second.isupper():
                return Address(city=city, state=second, country="US")
            return Address(city=city, country=second)
        if len(parts) >= 3:
            return Address(city=parts[0], state=parts[1], country=parts[2])
    return None


def _looks_like_contact_line(line: str) -> bool:
    lowered = line.lower()
    return bool(
        EMAIL_RE.search(line)
        or PHONE_RE.search(line)
        or "linkedin.com" in lowered
        or "github.com" in lowered
        or "http://" in lowered
        or "https://" in lowered
    )


def _extract_headline(top_lines: list[str], first_name: str, last_name: str) -> str | None:
    full_name = f"{first_name} {last_name}".strip().lower()
    for line in top_lines[:8]:
        lowered = line.lower().strip()
        if not lowered or lowered == full_name:
            continue
        if _looks_like_contact_line(line) or "@" in line:
            continue
        if line.count(",") >= 1 and not any(keyword in lowered for keyword in TITLE_HINTS):
            continue
        if 3 <= len(line) <= 90:
            return line
    return None


def _extract_summary(sections: dict[str, list[str]], top_lines: list[str]) -> str | None:
    summary_blocks = _split_into_blocks(sections.get("summary", []))
    if summary_blocks:
        summary = " ".join(summary_blocks[0])
        return summary[:600]

    header_candidates = [
        line
        for line in top_lines[2:8]
        if line
        and not _looks_like_contact_line(line)
        and len(line.split()) > 6
        and not _looks_like_location_only(line)
    ]
    if header_candidates:
        return " ".join(header_candidates[:2])[:600]
    return None


def _looks_like_location_only(line: str) -> bool:
    return "," in line and not any(char.isdigit() for char in line) and len(line.split()) <= 6


def _extract_list_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line:
            continue
        raw_parts = re.split(r"[,\n;|]", line)
        for part in raw_parts:
            item = part.strip().strip("-").strip()
            if not item:
                continue
            if len(item) > 40 or any(char.isdigit() for char in item):
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            items.append(item)
    return items[:30]


def _extract_languages(lines: list[str]) -> list[str]:
    languages = _extract_list_items(lines)
    return languages[:10]


def _extract_work_experience(lines: list[str]) -> list[WorkExperience]:
    experiences: list[WorkExperience] = []
    for block in _split_into_blocks(lines):
        entry = _parse_experience_block(block)
        if entry:
            experiences.append(entry)
    return experiences


def _parse_experience_block(block: list[str]) -> WorkExperience | None:
    lines = [_clean_bullet(line) for line in block if line.strip()]
    if len(lines) < 2:
        return None

    date_index = next((index for index, line in enumerate(lines) if _looks_like_date_line(line)), None)
    if date_index is None:
        return None

    start_date, end_date = _parse_date_range(lines[date_index])
    if start_date is None:
        return None

    header_lines = [line for index, line in enumerate(lines[: max(2, date_index + 1)]) if index != date_index]
    location = next(
        (
            line
            for index, line in enumerate(lines)
            if index != date_index and _looks_like_location_line(line)
        ),
        None,
    )
    if location:
        header_lines = [line for line in header_lines if line != location]

    title, company = _infer_title_and_company(header_lines)
    if not title or not company:
        return None

    description_lines = [line for index, line in enumerate(lines) if index > date_index and line != location]
    description = " ".join(description_lines).strip() or None

    return WorkExperience(
        company=company,
        title=title,
        start_date=start_date,
        end_date=end_date,
        description=description,
        location=location,
    )


def _clean_bullet(line: str) -> str:
    return line.lstrip("-*• ").strip()


def _looks_like_date_line(line: str) -> bool:
    lowered = line.lower()
    has_month = any(month in lowered for month in MONTHS)
    has_years = len(YEAR_RE.findall(line)) >= 1
    has_range = any(token in lowered for token in {" - ", " to ", "present", "current", "now"})
    return has_years and (has_month or has_range)


def _parse_date_range(line: str) -> tuple[str | None, str | None]:
    lowered = line.lower()
    token_pattern = (
        r"(present|current|now|\d{4}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        r"[a-z]*\s+\d{4}|\d{4})"
    )
    tokens = [match.group(0) for match in re.finditer(token_pattern, lowered, re.IGNORECASE)]
    if not tokens:
        return None, None

    start_date = _normalize_date_token(tokens[0])
    end_date = None
    if len(tokens) > 1:
        if tokens[1] not in {"present", "current", "now"}:
            end_date = _normalize_date_token(tokens[1])
    return start_date, end_date


def _normalize_date_token(token: str) -> str | None:
    value = token.strip().lower()
    if value in {"present", "current", "now"}:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{4}", value):
        return f"{value}-01"
    parts = value.split()
    if len(parts) == 2 and parts[0] in MONTHS and re.fullmatch(r"\d{4}", parts[1]):
        return f"{parts[1]}-{MONTHS[parts[0]]:02d}"
    return None


def _looks_like_location_line(line: str) -> bool:
    lowered = line.lower()
    if "remote" in lowered:
        return True
    if "," not in line:
        return False
    if any(char.isdigit() for char in line):
        return False
    return len(line.split()) <= 8


def _infer_title_and_company(lines: list[str]) -> tuple[str | None, str | None]:
    cleaned = [line for line in lines if line]
    if not cleaned:
        return None, None

    for line in cleaned:
        if " at " in line.lower():
            title, company = re.split(r"\s+at\s+", line, maxsplit=1, flags=re.IGNORECASE)
            return title.strip(), company.strip()

    if len(cleaned) >= 2:
        first, second = cleaned[0], cleaned[1]
        if _looks_like_title(first) and not _looks_like_title(second):
            return first, second
        if _looks_like_title(second) and not _looks_like_title(first):
            return second, first
        if _looks_like_company(first) and not _looks_like_company(second):
            return second, first
        if _looks_like_company(second) and not _looks_like_company(first):
            return first, second
        return first, second

    parts = [part.strip() for part in re.split(r"\s+\|\s+|\s+-\s+", cleaned[0]) if part.strip()]
    if len(parts) >= 2:
        first, second = parts[0], parts[1]
        if _looks_like_company(first) and not _looks_like_company(second):
            return second, first
        return first, second

    return None, None


def _looks_like_title(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in TITLE_HINTS)


def _looks_like_company(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in COMPANY_HINTS)


def _extract_education(lines: list[str]) -> list[Education]:
    education_items: list[Education] = []
    for block in _split_into_blocks(lines):
        entry = _parse_education_block(block)
        if entry:
            education_items.append(entry)
    return education_items


def _parse_education_block(block: list[str]) -> Education | None:
    lines = [_clean_bullet(line) for line in block if line.strip()]
    if len(lines) < 2:
        return None

    date_index = next((index for index, line in enumerate(lines) if _looks_like_date_line(line)), None)
    date_line = lines[date_index] if date_index is not None else ""
    start_date, end_date = _parse_date_range(date_line) if date_line else (None, None)

    header_lines = [line for index, line in enumerate(lines[: max(2, (date_index or 0) + 1)]) if index != date_index]
    institution = next((line for line in header_lines if _looks_like_institution(line)), None)
    degree_line = next((line for line in header_lines if _looks_like_degree(line)), None)

    if institution is None and header_lines:
        institution = header_lines[0]
    if degree_line is None and len(header_lines) > 1:
        degree_line = header_lines[1]

    if not institution or not degree_line:
        return None

    field_of_study = _extract_field_of_study(degree_line)

    return Education(
        institution=institution,
        degree=degree_line,
        field_of_study=field_of_study,
        start_date=start_date,
        end_date=end_date,
    )


def _looks_like_institution(value: str) -> bool:
    lowered = value.lower()
    return "university" in lowered or "college" in lowered or "school" in lowered or "institute" in lowered


def _looks_like_degree(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in DEGREE_HINTS)


def _extract_field_of_study(value: str) -> str | None:
    lowered = value.lower()
    for hint in FIELD_HINTS:
        if hint in lowered:
            return hint.title()

    if " in " in lowered:
        return value.split(" in ", 1)[1].strip()
    if "," in value:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if len(parts) > 1:
            return parts[-1]
    return None


def _extract_years_of_experience(text: str, work_experience: list[WorkExperience]) -> int | None:
    match = YEARS_OF_EXPERIENCE_RE.search(text)
    if match:
        return int(match.group(1))

    if not work_experience:
        return None

    parsed_starts = [_year_month_from_string(item.start_date) for item in work_experience]
    valid_starts = [item for item in parsed_starts if item is not None]
    if not valid_starts:
        return None

    earliest_year, earliest_month = min(valid_starts)
    today = date.today()
    months = (today.year - earliest_year) * 12 + (today.month - earliest_month)
    return max(0, months // 12)


def _year_month_from_string(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _split_into_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks
