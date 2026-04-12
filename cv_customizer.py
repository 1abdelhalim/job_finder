"""Automated CV customization engine.

Reads life-story.md + job description, generates tailored LaTeX files
(employment.tex, skills.tex, projects.tex), compiles to PDF.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from llm import generate_latex, generate_structured, check_ollama_available

logger = logging.getLogger(__name__)

# Project root — life-story.md lives here by default
_PROJECT_ROOT = Path(__file__).parent

# Default CV directory: ./cv/ inside the project.
# Override via `cv_dir` in profile.yaml (supports ~ expansion and absolute paths).
_DEFAULT_CV_DIR = _PROJECT_ROOT / "cv"

# Stable files to symlink from cv/ into each per-application directory
SYMLINK_FILES = [
    "education.tex", "teaching.tex", "publications.tex", "misc.tex",
    "referee.tex", "referee-full.tex",
    "own-bib.bib", "photo.png", "photo.jpg", "settings.sty",
]


def resolve_cv_dir(profile: Optional[dict] = None) -> Path:
    """Return the CV directory from profile config, falling back to ./cv."""
    raw = (profile or {}).get("pipeline", {}).get("cv_dir", "")
    if raw:
        return Path(os.path.expanduser(raw))
    return _DEFAULT_CV_DIR


def resolve_life_story_path(cv_dir: Path) -> Path:
    """Return the life-story.md path — project root takes priority over cv_dir."""
    project = _PROJECT_ROOT / "life-story.md"
    return project if project.exists() else cv_dir / "life-story.md"


# ---------------------------------------------------------------------------
# Back-compat module-level accessors (used by cmd_customize in main.py)
# ---------------------------------------------------------------------------
CV_DIR = _DEFAULT_CV_DIR
LIFE_STORY_PATH = resolve_life_story_path(CV_DIR)


def _read_file(path: Path) -> str:
    """Read a file, return empty string if not found."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("File not found: %s", path)
        return ""


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s]+', '-', text)
    return text[:60]


def _extract_user_name(life_story: str) -> str:
    """Extract the user's full name from the life story."""
    for line in life_story.splitlines()[:20]:
        m = re.search(r'\*\*Full Name:\*\*\s*(.+)', line)
        if m:
            return m.group(1).strip()
    # Fallback: first H1 heading
    m = re.search(r'^#\s+Life Story\s*[—–-]\s*(.+)', life_story, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return "the candidate"


def analyze_job(description: str, title: str = "", company: str = "", model: str = "qwen3.5:9b") -> Dict:
    """Analyze a job description to extract domain, key skills, and keywords.

    Returns dict with: domain, key_technologies, keywords, focus_areas
    """
    prompt = f"""Analyze this job posting and return a JSON object.

Job Title: {title}
Company: {company}

Job Description:
{description[:3000]}

Return JSON with these fields:
- "domain": one of ["3d_vision", "robotics_perception", "vlm_multimodal", "generative_ai", "nlp", "general_ml", "autonomous_driving"]
- "key_technologies": list of 5-10 specific technologies/frameworks mentioned or implied
- "keywords": list of 5-10 important keywords for this role
- "focus_areas": list of 3-5 main focus areas of the role
- "company_mission": one sentence about what the company does (if inferrable)
"""
    result = generate_structured(prompt, model=model)
    if not result:
        result = {
            "domain": "general_ml",
            "key_technologies": [],
            "keywords": [],
            "focus_areas": [],
            "company_mission": "",
        }
    return result


def generate_employment_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized employment.tex for a specific job."""

    # Pick the closest few-shot example
    domain = job_analysis.get("domain", "general_ml")
    example_path = EXAMPLE_EMPLOYMENT.get(domain)
    if not example_path:
        example_path = EXAMPLE_EMPLOYMENT.get("3d_vision")
    example = _read_file(example_path) if example_path and example_path.exists() else ""

    user_name = _extract_user_name(life_story)

    system = f"""You are an expert CV writer helping {user_name} apply for jobs.
You produce LaTeX code using the 'curve' document class rubric format.
You MUST output ONLY valid LaTeX — no markdown, no explanations, no code fences.
The output must compile with pdflatex without errors."""

    prompt = f"""Customize the employment section of {user_name}'s CV for this specific job.

TARGET JOB ANALYSIS:
- Domain: {job_analysis.get('domain', 'general_ml')}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}
- Keywords: {', '.join(job_analysis.get('keywords', []))}

MASTER SOURCE OF TRUTH ({user_name}'s full background):
{life_story[:6000]}

BASE TEMPLATE (default employment.tex — keep ALL entries present here):
{base_template}

{"EXAMPLE of a customized version for a similar domain:" if example else ""}
{example}

RULES:
1. Keep ALL job entries that appear in the base template — do not add or remove entries
2. Keep the exact same dates and job titles from the base template
3. HEAVILY customize the most recent/relevant entry to emphasize technologies relevant to the target job
4. Lightly adjust other entries to emphasize relevant aspects
5. Use \\textbf{{}} for key achievements and metrics
6. Use \\par to start description paragraphs
7. Escape special LaTeX characters: & → \\&, % → \\%, $ → use math mode
8. Use the EXACT format: \\begin{{rubric}}{{Experience}} ... \\entry*[dates]% ... \\end{{rubric}}
9. Do NOT add any text outside the rubric environment

Output the complete employment.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=3000)

    # Validate basic structure
    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning("Generated employment.tex missing rubric structure, using base template")
        return base_template

    return result


def generate_skills_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized skills.tex for a specific job."""

    domain = job_analysis.get("domain", "general_ml")
    example_path = EXAMPLE_SKILLS.get(domain)
    if not example_path:
        example_path = EXAMPLE_SKILLS.get("perception")
    example = _read_file(example_path) if example_path and example_path.exists() else ""

    user_name = _extract_user_name(life_story)

    system = f"""You are an expert CV writer helping {user_name} apply for jobs.
You produce LaTeX code using the 'curve' document class rubric format.
Output ONLY valid LaTeX — no markdown, no explanations, no code fences."""

    prompt = f"""Customize the skills section of {user_name}'s CV for this specific job.

TARGET JOB ANALYSIS:
- Domain: {job_analysis.get('domain', 'general_ml')}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}

AHMED'S FULL SKILLS (from life-story):
{life_story[:4000]}

BASE TEMPLATE:
{base_template}

{"EXAMPLE of a customized version for a similar domain:" if example else ""}
{example}

RULES:
1. Keep 4-5 skill categories using \\entry*[Category Name]%
2. REORDER categories so the most job-relevant one comes first (after Programming)
3. RENAME categories to match the job domain (e.g., "Computer Vision & 3D" for vision roles, "Vision & Language Models" for VLM roles)
4. Within each category, put the most job-relevant skills FIRST
5. Always keep Programming as the first entry
6. Always keep Awards as the last entry (keep awards content unchanged)
7. Use \\& for ampersand, \\LaTeX for LaTeX
8. Use the EXACT format: \\begin{{rubric}}{{Skills}} ... \\entry*[...]% ... \\end{{rubric}}

Output the complete skills.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=2000)

    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning("Generated skills.tex missing rubric structure, using base template")
        return base_template

    return result


def generate_projects_tex(
    life_story: str,
    job_analysis: Dict,
    base_template: str,
    model: str = "qwen3.5:9b",
) -> str:
    """Generate a customized projects.tex for a specific job."""

    user_name = _extract_user_name(life_story)

    system = f"""You are an expert CV writer helping {user_name} apply for jobs.
You produce LaTeX code using the 'curve' document class rubric format.
Output ONLY valid LaTeX — no markdown, no explanations, no code fences."""

    prompt = f"""Customize the projects section of {user_name}'s CV for this specific job.

TARGET JOB:
- Domain: {job_analysis.get('domain', 'general_ml')}
- Key Technologies: {', '.join(job_analysis.get('key_technologies', []))}
- Focus Areas: {', '.join(job_analysis.get('focus_areas', []))}

ALL AVAILABLE PROJECTS (from life-story):
{life_story[:4000]}

BASE TEMPLATE (current projects.tex):
{base_template}

RULES:
1. Select the 3-4 MOST RELEVANT projects for the target job
2. Order them by relevance (most relevant first)
3. Lightly adjust descriptions to emphasize job-relevant aspects
4. Keep \\href{{}}{{\\faGithub}} links intact
5. Use \\entry*[year]% format
6. Use \\begin{{rubric}}{{Projects}} ... \\end{{rubric}}

Output the complete projects.tex content:"""

    result = generate_latex(prompt, system=system, model=model, max_tokens=2000)

    if "\\begin{rubric}" not in result or "\\end{rubric}" not in result:
        logger.warning("Generated projects.tex missing rubric structure, using base template")
        return base_template

    return result


def validate_latex(content: str) -> bool:
    """Basic validation of LaTeX content."""
    # Check matching rubric environment
    opens = content.count("\\begin{rubric}")
    closes = content.count("\\end{rubric}")
    if opens != closes or opens == 0:
        return False

    # Check for common issues
    if content.count("{") != content.count("}"):
        # Allow small mismatches from escaped braces
        diff = abs(content.count("{") - content.count("}"))
        if diff > 2:
            return False

    return True


def create_application_dir(slug: str, cv_dir: Path) -> Path:
    """Create an application directory with copies and symlinks."""
    applications_dir = cv_dir / "applications"
    dest = applications_dir / slug
    if dest.exists():
        logger.info("Application dir already exists: %s", dest)
        return dest

    dest.mkdir(parents=True, exist_ok=True)

    # Copy the main CV tex file
    for f in ["cv-llt.tex"]:
        src = cv_dir / f
        if src.exists():
            shutil.copy2(str(src), str(dest / f))

    # Create symlinks back to cv_dir for stable (non-customized) files
    for f in SYMLINK_FILES:
        link = dest / f
        target = cv_dir / f
        if not link.exists() and target.exists():
            try:
                link.symlink_to(target)
            except OSError as e:
                logger.warning("Failed to create symlink %s: %s", f, e)

    return dest


def compile_latex(directory: Path) -> Optional[str]:
    """Compile LaTeX to PDF in the given directory. Returns PDF path or None."""
    tex_file = directory / "cv-llt.tex"
    if not tex_file.exists():
        logger.error("No cv-llt.tex found in %s", directory)
        return None

    try:
        result = subprocess.run(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "cv-llt.tex"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=120,
        )

        pdf_path = directory / "cv-llt.pdf"
        if pdf_path.exists():
            # Clean up auxiliary files
            for ext in [".aux", ".bbl", ".bcf", ".blg", ".fdb_latexmk", ".fls",
                        ".log", ".out", ".run.xml", ".synctex.gz", ".toc"]:
                aux = directory / ("cv-llt" + ext)
                if aux.exists():
                    aux.unlink()
            return str(pdf_path)
        else:
            logger.error("PDF not generated. LaTeX output:\n%s", result.stderr[-2000:])
            return None
    except subprocess.TimeoutExpired:
        logger.error("LaTeX compilation timed out")
        return None
    except FileNotFoundError:
        logger.error("latexmk not found. Install LaTeX (e.g., brew install --cask mactex)")
        return None


def customize_cv_for_job(
    job_url: str,
    title: str,
    company: str,
    location: str,
    description: str,
    model: str = "qwen3.5:9b",
    profile: Optional[dict] = None,
) -> Optional[Dict]:
    """Full CV customization pipeline for a job.

    Returns dict with: slug, cv_pdf_path, app_dir, or None on failure.
    """
    if not check_ollama_available():
        logger.error(
            "Ollama is not running. CV customization requires a local LLM.\n"
            "Install and start it: bash setup_ollama.sh"
        )
        return None

    cv_dir = resolve_cv_dir(profile)
    life_story_path = resolve_life_story_path(cv_dir)

    # Load master content
    life_story = _read_file(life_story_path)
    if not life_story:
        logger.error(
            "life-story.md not found. Expected at: %s\n"
            "Copy cv_templates/life_story_template.md to that path and fill it in.",
            life_story_path,
        )
        return None

    base_employment = _read_file(cv_dir / "employment.tex")
    base_skills = _read_file(cv_dir / "skills.tex")
    base_projects = _read_file(cv_dir / "projects.tex")

    # Create slug
    slug = _slugify(f"{company}-{title}")
    if not slug:
        slug = _slugify(company or "unknown")

    logger.info("Customizing CV for: %s at %s (slug: %s)", title, company, slug)

    # Step 1: Analyze job
    logger.info("Step 1: Analyzing job description...")
    job_analysis = analyze_job(description, title, company, model=model)
    logger.info("Job domain: %s", job_analysis.get("domain"))

    # Step 2: Generate customized LaTeX files with retry
    for attempt in range(3):
        logger.info("Step 2: Generating LaTeX (attempt %d)...", attempt + 1)

        employment_tex = generate_employment_tex(life_story, job_analysis, base_employment, model=model)
        skills_tex = generate_skills_tex(life_story, job_analysis, base_skills, model=model)
        projects_tex = generate_projects_tex(life_story, job_analysis, base_projects, model=model)

        # Validate
        valid = all([
            validate_latex(employment_tex),
            validate_latex(skills_tex),
            validate_latex(projects_tex),
        ])

        if valid:
            break
        logger.warning("LaTeX validation failed on attempt %d", attempt + 1)

        if attempt == 2:
            logger.warning("Using base templates as fallback")
            employment_tex = base_employment
            skills_tex = base_skills
            projects_tex = base_projects

    # Step 3: Create application directory
    logger.info("Step 3: Creating application directory...")
    app_dir = create_application_dir(slug, cv_dir)

    # Write customized files
    (app_dir / "employment.tex").write_text(employment_tex, encoding="utf-8")
    (app_dir / "skills.tex").write_text(skills_tex, encoding="utf-8")
    (app_dir / "projects.tex").write_text(projects_tex, encoding="utf-8")

    # Write job description for reference
    jd_content = f"# {title} at {company}\n\n**Location:** {location}\n**URL:** {job_url}\n\n---\n\n{description}"
    (app_dir / "job-description.md").write_text(jd_content, encoding="utf-8")

    # Step 4: Compile to PDF
    logger.info("Step 4: Compiling LaTeX to PDF...")
    pdf_path = compile_latex(app_dir)

    if pdf_path:
        logger.info("CV generated: %s", pdf_path)
        return {
            "slug": slug,
            "cv_pdf_path": pdf_path,
            "app_dir": str(app_dir),
        }
    else:
        logger.error("PDF compilation failed for %s", slug)
        # Try with base templates
        logger.info("Retrying with base templates...")
        (app_dir / "employment.tex").write_text(base_employment, encoding="utf-8")
        (app_dir / "skills.tex").write_text(base_skills, encoding="utf-8")
        (app_dir / "projects.tex").write_text(base_projects, encoding="utf-8")
        pdf_path = compile_latex(app_dir)
        if pdf_path:
            logger.warning("Used base templates as fallback. PDF at: %s", pdf_path)
            return {
                "slug": slug,
                "cv_pdf_path": pdf_path,
                "app_dir": str(app_dir),
            }
        return None
