# CV Templates

This directory contains blank LaTeX CV templates that work with the job_finder CV customization pipeline. Three styles are included:

| File | Style | Use when |
|---|---|---|
| `cv-llt-template.tex` | Two-page academic (curve class) | Research / academic applications |
| `cv-single-page-template.tex` | Single-page two-column | Industry applications |
| `cv-ats-safe-template.tex` | Plain single-column | ATS-heavy portals (Workday, Taleo, Greenhouse) |

Content section templates (used by `cv-llt-template.tex`):
- `employment-template.tex` — work experience
- `education-template.tex` — degrees
- `skills-template.tex` — skills
- `projects-template.tex` — side projects

---

## 1. Install LaTeX

### macOS
```bash
# Full MacTeX (~4 GB, includes everything):
brew install --cask mactex

# Or smaller BasicTeX + required packages (~100 MB):
brew install --cask basictex
sudo tlmgr update --self
sudo tlmgr install latexmk biber biblatex \
    fontawesome5 cochineal cabin zi4 \
    paracol xcolor tikz pgf geometry \
    hyperref microtype relsize comment \
    titlesec enumitem lmodern
```

### Linux (Debian/Ubuntu)
```bash
sudo apt-get install texlive-full latexmk biber
# Or minimal:
sudo apt-get install texlive-latex-recommended texlive-latex-extra \
    texlive-fonts-recommended texlive-bibtex-extra latexmk biber
```

### Windows
Download and install [MiKTeX](https://miktex.org/download) or [TeX Live](https://tug.org/texlive/).
Make sure `latexmk` and `biber` are available in PATH.

---

## 2. Set Up Your CV Directory

The system expects your CV files to live in `~/CV/` (configurable via `cv_dir` in `profile.yaml`).

```bash
mkdir -p ~/CV ~/CV/applications

# Copy the templates you want to use:
cp cv_templates/cv-llt-template.tex     ~/CV/cv-llt.tex
cp cv_templates/employment-template.tex ~/CV/employment.tex
cp cv_templates/education-template.tex  ~/CV/education.tex
cp cv_templates/skills-template.tex     ~/CV/skills.tex
cp cv_templates/projects-template.tex   ~/CV/projects.tex

# Copy the required style file from this repo:
cp cv_templates/settings.sty            ~/CV/settings.sty

# Copy the life story template:
cp cv_templates/life_story_template.md  ~/CV/life-story.md

# For single-page or ATS versions:
cp cv_templates/cv-single-page-template.tex ~/CV/cv-single-page.tex
cp cv_templates/cv-ats-safe-template.tex    ~/CV/cv-ats-safe.tex

# Create an empty publications bib file if you don't have publications:
touch ~/CV/own-bib.bib
```

---

## 3. Fill In Your Content

### Step 1 — Write your life story
Open `~/CV/life-story.md` and fill it in completely. This is the **single source of truth** the LLM reads to generate your tailored CV. Be detailed and honest — include:
- Every job with responsibilities and achievements (numbers/metrics)
- All projects with what they do and technologies used
- Education history
- Publications / papers

### Step 2 — Generate your profile.yaml (optional, LLM-assisted)
If Ollama/Qwen is available:
```bash
python main.py init-profile --life-story ~/CV/life-story.md --output profile.yaml
```
This reads your life story and generates a `profile.yaml` with skills, titles, and keywords pre-filled. Review and edit the output before use.

Or copy and fill in `profile.yaml.example` manually.

### Step 3 — Fill in the LaTeX templates
Edit the `.tex` files in `~/CV/` — replace all `YOUR_*` placeholders with your actual information. For `cv-llt.tex`, edit the content files (`employment.tex`, `skills.tex`, etc.) separately.

### Step 4 — Compile
```bash
# Two-page LLT (uses biber for references):
cd ~/CV
latexmk -pdf cv-llt.tex

# Or manually:
pdflatex cv-llt.tex && biber cv-llt && pdflatex cv-llt.tex && pdflatex cv-llt.tex

# Single-page:
pdflatex cv-single-page.tex && pdflatex cv-single-page.tex

# ATS-safe:
pdflatex cv-ats-safe.tex
```

---

## 4. How CV Customization Works

When the pipeline finds a high-matching job, it:

1. Reads `~/CV/life-story.md` as the source of truth
2. Uses Qwen (via Ollama) to rewrite `employment.tex`, `skills.tex`, and `projects.tex` to emphasize experience relevant to that job
3. Compiles to PDF using `latexmk`
4. Saves the result in `~/CV/applications/<company-slug>/`

**The base files (`employment.tex`, `skills.tex`, `projects.tex`) are never overwritten** — customized copies go into the per-application directory.

### LLM requirement
CV customization requires Ollama with a compatible model:
- **GPU / Apple Silicon:** `qwen3.5:9b` (~5 GB)
- **CPU only:** `qwen2.5:3b` (~2 GB, needs ≥8 GB RAM)

```bash
bash setup_ollama.sh   # installs the right model for your hardware
```

If Ollama is not running or not installed, **job scraping and matching still work** — only CV customization is disabled.

---

## 5. Adding a Photo

Place a file named `photo.png` (or `photo.jpg`) in `~/CV/`. For the LLT template, it appears in the header when `\includecomment{fullonly}` is active. For the single-page template, uncomment the `\includegraphics` line.

Professional headshot, square crop, ~300×300px minimum recommended.

---

## 6. Colour & Font Customization

In `cv-llt.tex` or `cv-single-page.tex`:
```latex
% Change accent colours:
\definecolor{SwishLineColour}{HTML}{003580}   % line colour
\definecolor{MarkerColour}{HTML}{B6073F}       % bookmark marker

% Change page margins:
\geometry{left=1.5cm,right=1.5cm,top=1.5cm,bottom=1.5cm}
```

In `settings.sty` you can further customize fonts, spacing, and header layout.

---

## 7. Troubleshooting

| Error | Fix |
|---|---|
| `curve.cls not found` | Install the curve class: `tlmgr install curve` |
| `fontawesome5.sty not found` | `tlmgr install fontawesome5` |
| `cochineal.sty not found` | `tlmgr install cochineal` |
| `biber not found` | `brew install biber` or `apt install biber` |
| `latexmk not found` | `brew install latexmk` or `apt install latexmk` |
| PDF not generated | Check the `.log` file for errors |
| Publication names not bolded | Check `\mynames{}` spelling matches your `.bib` entries exactly |
