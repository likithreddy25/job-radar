"""Resume packet builder for scanned and manually pasted jobs."""
from __future__ import annotations

from pathlib import Path
import re

from .evaluation import EvaluationResult
from .profile import PROFILE, SKILLS_MODERATE, SKILLS_STRONG

ROOT_DIR = Path(__file__).resolve().parent.parent
LOCAL_BASE_RESUME_PATH = ROOT_DIR / "data" / "resume" / "base_resume.local.md"
BASE_RESUME_PATH = ROOT_DIR / "data" / "resume" / "base_resume.md"
PROMPT_TEMPLATE_PATH = ROOT_DIR / "data" / "resume" / "master_resume_prompt.md"
LOCAL_LATEX_TEMPLATE_PATH = ROOT_DIR / "data" / "resume" / "RESUME_TEMPLATE.local.tex"
LATEX_TEMPLATE_PATH = ROOT_DIR / "data" / "resume" / "RESUME_TEMPLATE.tex"

_PROJECTS_SECTION_RE = re.compile(
    r"(\\section\*\{Selected Projects\}\s*)(?P<body>.*?)(\n\s*% ============================================================\n\s*% OPTIONAL PUBLICATION)",
    re.DOTALL,
)


def _default_base_resume() -> str:
    signature_name = str(PROFILE.get("name") or "Candidate Name")
    return "\n".join(
        [
            f"# {PROFILE['name']}",
            f"{PROFILE['email']} | {PROFILE['location']}",
            "",
            "## Professional Summary",
            (
                f"{PROFILE['experience_years']} years of experience in data and analytics. "
                "Update this section with your actual summary, domain expertise, and strongest outcomes."
            ),
            "",
            "## Core Skills",
            ", ".join(sorted(SKILLS_STRONG)[:18]),
            "",
            "## Experience",
            "- Replace this with your real work experience bullets.",
            "- Use quantified impact, tools used, and business outcomes.",
            "",
            "## Projects",
            "- Replace this with your strongest projects.",
            "",
            "## Education",
            f"- {PROFILE['education']}",
            "",
        ]
    ).strip() + "\n"


def _default_prompt_template() -> str:
    return "\n".join(
        [
            "# MASTER RESUME PROMPT",
            "",
            "Use the attached `cv_master.md`, `RESUME_TEMPLATE.tex`, and the pasted job description to build a one-page tailored resume and cover letter.",
            "",
            "Hard requirements:",
            "- Keep the LaTeX template structure unchanged.",
            "- Tailor bullets and summary to the JD without inflating titles.",
            "- Use exact JD keywords in experience bullets when defensible.",
            "- Keep claims defensible and ATS-safe.",
        ]
    ).strip() + "\n"


def _default_latex_template() -> str:
    return "\n".join(
        [
            r"\documentclass[letterpaper,9pt]{article}",
            r"\begin{document}",
            r"Replace with the stored RESUME_TEMPLATE.tex content.",
            r"\end{document}",
        ]
    ).strip() + "\n"


def _read_preferred_text(primary: Path, fallback: Path | None = None, *, default: str) -> tuple[str, Path | None]:
    for path in [primary, fallback]:
        if path is None or not path.exists():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text + "\n", path
    return default, None


def load_base_resume_markdown() -> tuple[str, Path | None]:
    return _read_preferred_text(
        LOCAL_BASE_RESUME_PATH,
        BASE_RESUME_PATH,
        default=_default_base_resume(),
    )


def load_prompt_template() -> tuple[str, Path | None]:
    return _read_preferred_text(
        PROMPT_TEMPLATE_PATH,
        default=_default_prompt_template(),
    )


def load_latex_template() -> tuple[str, Path | None]:
    return _read_preferred_text(
        LOCAL_LATEX_TEMPLATE_PATH,
        LATEX_TEMPLATE_PATH,
        default=_default_latex_template(),
    )


def _job_description_markdown(job: dict) -> str:
    title = (job.get("title") or "Target Role").strip()
    company = (job.get("company") or "Target Company").strip()
    location = (job.get("location") or "").strip() or "Unknown"
    url = (job.get("url") or "").strip() or "N/A"
    source = (job.get("source") or "").strip() or "unknown"
    description = (job.get("description") or "").strip() or "No job description stored."
    return "\n".join(
        [
            f"# Job Description -- {company} -- {title}",
            f"**Location:** {location}",
            f"**Source:** {source}",
            f"**Link:** {url}",
            "",
            "## Raw Job Description",
            description,
        ]
    ).strip() + "\n"


def _extract_markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group("body").strip() if match else ""


def _extract_subsection_block(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^###\s+|^##\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    return match.group("body").strip() if match else ""


def _extract_bullets(block: str) -> list[str]:
    return [
        line.strip()[2:].strip()
        for line in block.splitlines()
        if line.strip().startswith("- ")
    ]


def _extract_projects(markdown: str) -> list[dict[str, object]]:
    block = _extract_markdown_section(markdown, "Selected Projects")
    if not block:
        return []
    matches = re.finditer(
        r"^###\s+(?P<name>.+?)\n(?P<body>.*?)(?=^###\s+|\Z)",
        block,
        re.MULTILINE | re.DOTALL,
    )
    projects: list[dict[str, object]] = []
    for match in matches:
        name = match.group("name").strip()
        body = match.group("body")
        year_match = re.search(r"^\*\*(?P<year>.+?)\*\*\s*$", body, flags=re.MULTILINE)
        bullets = _extract_bullets(body)
        if name:
            projects.append(
                {
                    "name": name,
                    "year": (year_match.group("year").strip() if year_match else ""),
                    "bullets": bullets,
                }
            )
    return projects


def _latex_escape(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    escaped = "".join(replacements.get(ch, ch) for ch in (text or ""))
    return escaped.replace("—", "--").replace("–", "--")


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _phrase_present(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = _normalize_for_match(phrase)
    if not normalized_phrase:
        return False
    return re.search(rf"\b{re.escape(normalized_phrase)}\b", normalized_text) is not None


def _token_set(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(token) > 2}


def _priority_terms(job: dict, evaluation: EvaluationResult) -> list[str]:
    title = (job.get("title") or "").strip()
    description = (job.get("description") or "").strip()
    title_tokens = _token_set(title)
    description_tokens = _token_set(description)
    common_role_terms = {
        "data",
        "pipeline",
        "pipelines",
        "python",
        "sql",
        "schema",
        "semantic",
        "lakehouse",
        "etl",
        "elt",
        "dbt",
        "airflow",
        "databricks",
        "spark",
        "glue",
        "aws",
        "embedding",
        "embeddings",
        "vector",
        "metadata",
        "governance",
        "quality",
        "mlflow",
        "feature",
        "features",
        "products",
    }
    ordered_terms: list[str] = []
    for term in evaluation.matched_strong + evaluation.matched_moderate:
        if term not in ordered_terms:
            ordered_terms.append(term)
    for term in sorted((title_tokens & common_role_terms) | (description_tokens & common_role_terms)):
        if term not in ordered_terms:
            ordered_terms.append(term)
    return ordered_terms


def _score_text_against_job(text: str, job: dict, evaluation: EvaluationResult) -> int:
    normalized = _normalize_for_match(text)
    text_tokens = _token_set(text)
    title_tokens = _token_set(job.get("title") or "")
    description_tokens = _token_set(job.get("description") or "")
    score = 0
    score += len(text_tokens & title_tokens) * 4
    score += len(text_tokens & description_tokens) * 2
    for term in _priority_terms(job, evaluation):
        if _phrase_present(normalized, term):
            score += 12 if term in evaluation.matched_strong else 8
    if re.search(r"\b(dbt|airflow|spark|databricks|sql|python|aws|glue|mlflow|pipeline|etl|elt)\b", normalized):
        score += 8
    if re.search(r"\b(\d+%|\$\d+|\d+k\+|\d+\+)\b", text.lower()):
        score += 3
    return score


def _rank_bullets(bullets: list[str], job: dict, evaluation: EvaluationResult, *, limit: int) -> list[str]:
    ranked = sorted(
        bullets,
        key=lambda bullet: (_score_text_against_job(bullet, job, evaluation), len(bullet)),
        reverse=True,
    )
    return ranked[:limit]


def _rank_projects(projects: list[dict[str, object]], job: dict, evaluation: EvaluationResult, *, limit: int) -> list[dict[str, object]]:
    ranked = sorted(
        projects,
        key=lambda item: (
            _score_text_against_job(
                " ".join([str(item.get("name") or ""), " ".join(item.get("bullets") or [])]),
                job,
                evaluation,
            ),
            len(item.get("bullets") or []),
        ),
        reverse=True,
    )
    return ranked[:limit]


def _address_for_location(location: str) -> str:
    loc = (location or "").lower()
    addresses = PROFILE.get("location_addresses") or {}
    if any(token in loc for token in (" tx", "texas", "dallas", "plano", "irving", "houston", "austin")):
        return str(addresses.get("Texas") or PROFILE.get("location") or "Fairfax, Virginia")
    if any(token in loc for token in (" philadelphia", "pennsylvania", "downingtown", ", pa")):
        return str(addresses.get("Pennsylvania / Philadelphia") or PROFILE.get("location") or "Fairfax, Virginia")
    if any(token in loc for token in (" atlanta", "georgia", " marietta", ", ga")):
        return str(addresses.get("Atlanta / Georgia") or PROFILE.get("location") or "Fairfax, Virginia")
    if any(token in loc for token in (" ohio", "fairborn", "columbus", "cleveland", "cincinnati", ", oh")):
        return str(addresses.get("Ohio") or PROFILE.get("location") or "Fairfax, Virginia")
    return str(addresses.get("Fairfax, Virginia") or PROFILE.get("location") or "Fairfax, Virginia")


def _email_for_company(company: str) -> str:
    role_emails = PROFILE.get("role_emails") or {}
    company_key = (company or "").strip().lower()
    if company_key in {"amazon", "microsoft"}:
        preferred = role_emails.get("amazon_microsoft")
        if preferred:
            return str(preferred)
    return str(PROFILE.get("email") or "candidate@example.com")


def _best_metric_phrase(bullets: list[str]) -> str:
    for bullet in bullets:
        if "%" in bullet:
            return bullet
    for bullet in bullets:
        if "$" in bullet or re.search(r"\b\d+K\+\b", bullet):
            return bullet
    return bullets[0] if bullets else ""


def _summary_for_tex(job: dict, evaluation: EvaluationResult, selected_hades: list[str], selected_hive: list[str], fallback: str) -> str:
    title = (job.get("title") or "Target Role").strip()
    matched = evaluation.matched_strong[:4]
    metric_source = _best_metric_phrase(selected_hades + selected_hive)
    metric_text = ""
    if metric_source:
        metric_fragments = re.findall(r"(\d+%|\$\d+[A-Za-z+]*|\d+K\+|\d+\+\s*events per minute)", metric_source)
        if metric_fragments:
            metric_text = ", ".join(metric_fragments[:2])
    summary = f"{title} candidate with 2.5+ years building production data and AI systems."
    if matched:
        summary += " Strongest overlap in " + ", ".join(matched) + "."
    if metric_text:
        summary += f" Recent delivery includes {metric_text} impact across production pipelines and data products."
    elif fallback:
        summary += " " + fallback
    return summary.strip()


def _render_project_block(project: dict[str, object]) -> str:
    name = _latex_escape(str(project.get("name") or "Selected Project"))
    year = _latex_escape(str(project.get("year") or ""))
    bullets = [str(item) for item in (project.get("bullets") or []) if str(item).strip()]
    bullet = _latex_escape(bullets[0] if bullets else "Add one role-relevant project bullet here.")
    return "\n".join(
        [
            rf"\textbf{{{name}}} \hfill {year}",
            r"\begin{itemize}",
            rf"    \item {bullet}",
            r"\end{itemize}",
        ]
    )


def _fill_resume_template(
    latex_template: str,
    *,
    summary: str,
    address: str,
    email: str,
    hades_bullets: list[str],
    hive_bullets: list[str],
    project_items: list[dict[str, object]],
) -> str:
    filled = latex_template
    contact_phone = str(PROFILE.get("contact_phone") or "")
    linkedin_url = str(PROFILE.get("linkedin_url") or "")
    github_url = str(PROFILE.get("github_url") or "")
    candidate_name = str(PROFILE.get("name") or "Candidate Name")
    filled = filled.replace("CANDIDATE_NAME", _latex_escape(candidate_name))
    filled = filled.replace("FAIRFAX_OR_ROLE_SPECIFIC_ADDRESS", _latex_escape(address))
    filled = filled.replace("PHONE_FOR_THIS_ROLE", _latex_escape(contact_phone))
    filled = filled.replace("EMAIL_FOR_THIS_ROLE", _latex_escape(email))
    filled = filled.replace("LINKEDIN_FOR_THIS_ROLE", _latex_escape(linkedin_url))
    filled = filled.replace("GITHUB_FOR_THIS_ROLE", _latex_escape(github_url))
    filled = filled.replace("SUMMARY_TEXT_HERE", _latex_escape(summary))

    hades_defaults = hades_bullets + ["Built production data workflows with measurable business impact."] * 4
    for placeholder, value in zip(
        [
            "MOST_RELEVANT_BULLET_FOR_TARGET_ROLE",
            "SECOND_MOST_RELEVANT_BULLET",
            "THIRD_RELEVANT_BULLET",
            "OPTIONAL_FOURTH_BULLET",
        ],
        hades_defaults[:4],
    ):
        filled = filled.replace(placeholder, _latex_escape(value))

    hive_defaults = hive_bullets + ["Applied analytics to product and growth decisions."] * 3
    for placeholder, value in zip(
        [
            "MOST_RELEVANT_HIVE_BULLET",
            "SECOND_HIVE_BULLET",
            "THIRD_HIVE_BULLET",
        ],
        hive_defaults[:3],
    ):
        filled = filled.replace(placeholder, _latex_escape(value))

    selected_projects = project_items[:2]
    project_blocks = "\n\n".join(_render_project_block(project) for project in selected_projects)
    project_match = _PROJECTS_SECTION_RE.search(filled)
    if project_match:
        filled = (
            filled[: project_match.start("body")]
            + project_blocks
            + filled[project_match.end("body") :]
        )
    return filled


def _sentences(text: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", text or "") if segment.strip()]


def _truncate_at_word_boundary(text: str, limit: int = 220) -> str:
    cleaned = " ".join((text or "").split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    truncated = cleaned[:limit].rsplit(" ", 1)[0].strip()
    return truncated or cleaned[:limit].strip()


def _compact_focus_text(text: str) -> str:
    cleaned = " ".join((text or "").split()).strip()
    if len(cleaned) <= 150:
        return cleaned
    for marker in (" using ", " with ", " to "):
        if marker in cleaned.lower():
            parts = re.split(marker, cleaned, maxsplit=1, flags=re.IGNORECASE)
            if parts and parts[0].strip():
                return parts[0].strip()
    return _truncate_at_word_boundary(cleaned, limit=150)


def _best_role_focus(job: dict, evaluation: EvaluationResult) -> str:
    description = (job.get("description") or "").strip()
    if not description:
        return "building AI-ready data products and repeatable analytics infrastructure"
    skip_phrases = (
        "we unite caring with discovery",
        "global healthcare leader",
        "we give our best effort",
        "we put people first",
        "we are looking for people",
        "eeo employer",
        "actual compensation",
        "benefit program",
        "accommodation request",
        "we are lilly",
    )
    preferred_terms = _priority_terms(job, evaluation) + [
        "semantic layer",
        "data harmonization",
        "ai-ready data products",
        "lakehouse architecture",
        "etl",
        "elt",
        "vector embedding",
        "schema",
        "metadata",
    ]
    candidates: list[tuple[int, str]] = []
    for sentence in _sentences(description):
        normalized = _normalize_for_match(sentence)
        if any(phrase in normalized for phrase in skip_phrases):
            continue
        score = sum(1 for term in preferred_terms if _phrase_present(normalized, term))
        if score:
            candidates.append((score, sentence))
    if candidates:
        candidates.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return _compact_focus_text(candidates[0][1])
    for sentence in _sentences(description):
        normalized = _normalize_for_match(sentence)
        if not any(phrase in normalized for phrase in skip_phrases):
            return _compact_focus_text(sentence)
    return "building AI-ready data products and repeatable analytics infrastructure"


def _build_cover_letter(job: dict, evaluation: EvaluationResult, selected_hades: list[str], selected_hive: list[str]) -> str:
    company = (job.get("company") or "the company").strip()
    title = (job.get("title") or "the role").strip()
    matched = evaluation.matched_strong[:4]
    matched_text = ", ".join(matched[:3]) if matched else "data engineering, analytics engineering, and AI-ready pipeline work"
    role_focus = _best_role_focus(job, evaluation)
    hades_proof = selected_hades[0] if selected_hades else "I built end-to-end data and ML workflows across Python, SQL, and cloud data systems."
    hive_proof = selected_hive[0] if selected_hive else "I translated product and user data into measurable business outcomes."
    signature_name = str(PROFILE.get("name") or "Candidate Name")
    return "\n".join(
        [
            f"# Cover Letter -- {company} -- {title}",
            "",
            "Dear Hiring Team,",
            "",
            f"I am applying for the {title} role at {company}. The role's focus on {role_focus} matches the work I have been doing across production data systems, machine learning delivery, and analytics engineering.",
            "",
            f"At H.A.D.E.S, {hades_proof} At Hive Media, {hive_proof.lower()} Those experiences sharpened how I design data products around downstream use, operational reliability, and measurable delivery.",
            "",
            f"For this role, the strongest overlap is in {matched_text}. I can contribute to AI-ready data layers, reproducible pipelines, and data products that are useful to both scientists and engineers while staying grounded in maintainable systems and clear outcomes.",
            "",
            "Sincerely,",
            signature_name,
        ]
    ).strip() + "\n"


def generate_resume_packet(job: dict, evaluation: EvaluationResult) -> dict[str, object]:
    title = (job.get("title") or "Target Role").strip()
    company = (job.get("company") or "Target Company").strip()
    location = (job.get("location") or PROFILE.get("location", "")).strip()
    score = int(evaluation.score)
    label = (evaluation.label or "").upper()

    prompt_template, prompt_path = load_prompt_template()
    base_resume, resume_path = load_base_resume_markdown()
    latex_template, latex_path = load_latex_template()
    jd_markdown = _job_description_markdown(job)

    base_summary = _extract_markdown_section(base_resume, "Summary").replace("\n", " ").strip()
    hades_bullets = _extract_bullets(_extract_subsection_block(base_resume, "H.A.D.E.S - Data Scientist"))
    hive_bullets = _extract_bullets(_extract_subsection_block(base_resume, "Hive Media - Data Scientist, Growth Analytics"))
    projects = _extract_projects(base_resume)

    selected_hades = _rank_bullets(hades_bullets, job, evaluation, limit=4)
    selected_hive = _rank_bullets(hive_bullets, job, evaluation, limit=3)
    selected_projects = _rank_projects(projects, job, evaluation, limit=2)

    resume_tex = _fill_resume_template(
        latex_template,
        summary=_summary_for_tex(job, evaluation, selected_hades, selected_hive, base_summary),
        address=_address_for_location(location),
        email=_email_for_company(company),
        hades_bullets=selected_hades,
        hive_bullets=selected_hive,
        project_items=selected_projects,
    )
    cover_letter = _build_cover_letter(job, evaluation, selected_hades, selected_hive)

    matched_primary = evaluation.matched_strong[:12]
    matched_secondary = evaluation.matched_moderate[:10]
    remaining_primary = [skill for skill in sorted(SKILLS_STRONG) if skill not in matched_primary][:10]
    remaining_secondary = [skill for skill in sorted(SKILLS_MODERATE) if skill not in matched_secondary][:8]

    lines = [
        f"# Resume Packet -- {company} -- {title}",
        "",
        "Copy this packet into Claude, Gemini, or GPT. The prompt, master resume, LaTeX template, and JD are bundled below.",
        "",
        "## Job Radar Context",
        f"- Score: {score}/100 ({label})",
        f"- Company: {company}",
        f"- Role: {title}",
        f"- Location: {location or 'Unknown'}",
        f"- Matched core skills: {', '.join(matched_primary) if matched_primary else 'None detected from stored JD text.'}",
        f"- Supporting skills: {', '.join(matched_secondary) if matched_secondary else 'None detected from stored JD text.'}",
        f"- Additional skills to consider: {', '.join(remaining_primary[:6] + remaining_secondary[:4])}",
        "",
        "## Prompt Source",
        f"- Prompt template: `{(prompt_path or PROMPT_TEMPLATE_PATH).as_posix()}`",
        f"- Master resume source: `{(resume_path or BASE_RESUME_PATH).as_posix()}`",
        f"- LaTeX template source: `{(latex_path or LATEX_TEMPLATE_PATH).as_posix()}`",
        "",
        "## Saved Artifacts",
        "- `master_resume_prompt.md`",
        "- `JD.md`",
        "- `resume_draft.tex`",
        "- `cover_letter.md`",
        "",
        "## File: master_resume_prompt.md",
        "```md",
        prompt_template.rstrip(),
        "```",
        "",
        "## File: cv_master.md",
        "```md",
        base_resume.rstrip(),
        "```",
        "",
        "## File: RESUME_TEMPLATE.tex",
        "```tex",
        latex_template.rstrip(),
        "```",
        "",
        "## File: JD.md",
        "```md",
        jd_markdown.rstrip(),
        "```",
    ]
    artifacts = [
        {
            "name": "Prompt",
            "filename": "master_resume_prompt.md",
            "format": "markdown",
            "content": prompt_template.rstrip() + "\n",
        },
        {
            "name": "JD Markdown",
            "filename": "JD.md",
            "format": "markdown",
            "content": jd_markdown.rstrip() + "\n",
        },
        {
            "name": "Resume TeX",
            "filename": "resume_draft.tex",
            "format": "tex",
            "content": resume_tex.rstrip() + "\n",
        },
        {
            "name": "Cover Letter",
            "filename": "cover_letter.md",
            "format": "markdown",
            "content": cover_letter.rstrip() + "\n",
        },
    ]
    return {
        "bundle_markdown": "\n".join(lines).strip() + "\n",
        "artifacts": artifacts,
    }


def generate_resume_markdown(job: dict, evaluation: EvaluationResult) -> str:
    packet = generate_resume_packet(job, evaluation)
    return str(packet["bundle_markdown"])
