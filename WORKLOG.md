# Work Log

This file tracks code and behavior changes made in this repo during assistant sessions.

Conventions:
- Newest entries go at the top.
- Keep entries short and outcome-focused.
- Record changed files when practical.

## 2026-04-27

### Prompt packet content hardening
- Updated [src/resume_builder.py](/c:/Users/vasis/Desktop/job/job-radar/src/resume_builder.py) so generated `resume_draft.tex` now ranks and reorders experience bullets against the current JD instead of blindly preserving the base resume order.
- Updated [src/resume_builder.py](/c:/Users/vasis/Desktop/job/job-radar/src/resume_builder.py) so generated project output now uses exactly two ranked projects with real stored year values, instead of emitting a fake fallback third project placeholder.
- Updated [src/resume_builder.py](/c:/Users/vasis/Desktop/job/job-radar/src/resume_builder.py) so cover-letter intros skip generic company boilerplate and extract a shorter role-focus sentence from the actual technical responsibilities.

### Prompt packet page and artifact downloads
- Updated [src/database.py](/c:/Users/vasis/Desktop/job/job-radar/src/database.py) to store packet artifacts separately in a new `generated_artifacts` table linked to each generated packet.
- Updated [src/resume_builder.py](/c:/Users/vasis/Desktop/job/job-radar/src/resume_builder.py) so packet generation now returns separate saved artifacts for the prompt, JD markdown, generated resume `.tex`, and cover letter draft alongside the bundled packet markdown.
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so prompt generation now saves those separate artifacts, redirects to a dedicated `/packet` page, and exposes per-artifact `Copy` and `Download` actions.

### AI resume packet integration
- Replaced the old weak markdown addendum in [src/resume_builder.py](/c:/Users/vasis/Desktop/job/job-radar/src/resume_builder.py) with a packet builder that bundles a strict master prompt, the loaded master resume, the LaTeX template, and the current job description into one copy-ready artifact for Claude, Gemini, or GPT.
- Added [data/resume/master_resume_prompt.md](/c:/Users/vasis/Desktop/job/job-radar/data/resume/master_resume_prompt.md) as the wired-in prompt template for tailored one-page resume generation.
- Added [data/resume/RESUME_TEMPLATE.tex](/c:/Users/vasis/Desktop/job/job-radar/data/resume/RESUME_TEMPLATE.tex) so the prompt packet includes the exact LaTeX resume shell used for tailoring.
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so the job detail action now reads `Generate AI Resume Packet`, generated artifacts are labeled as resume packets in the UI, and new saves are tagged with format `prompt_packet`.

### Salary, internship, vendor, and US-filter hardening
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so salary extraction now rejects implausible annual ranges and broken tiny lower bounds instead of storing obviously wrong values like `$500 - $160,000/year` or billion-dollar max salaries.
- Tightened internship detection in [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so generic mentions of internships in a JD do not label senior Workday roles as `internship`.
- Expanded employer-quality penalties in [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) for consulting and vendor companies such as `Stott and May`, `BCforward`, `Capgemini`, `Ascendion`, and similar inventory.
- Hardened [src/sources/base.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/base.py) so mixed-country remote strings like `Remote - US ... Canada` and explicit non-US remote locations like `Remote - Denmark` are excluded from US-only views.

### Single-line local batch progress
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) so local boards-mode progress uses the single-line carriage-return bar more consistently instead of falling back to line-by-line `Sweep progress` logs when PowerShell does not report `isatty()` the way the app expects.
- Added a newline before the `Batch done` log so the completed progress bar and the next log line do not collide.

### JD similarity and employer-quality hardening
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so `description_similarity` now uses union-based token overlap instead of dividing by the smaller token set, which avoids inflated repost similarity on asymmetric job descriptions.
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so agency detection in `score_employer_quality` now uses targeted regex patterns instead of raw substring checks, preventing false hits like `IBM Consulting` or `Talentful`.
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so salary extraction scores all candidate range matches and prefers the most salary-like one instead of taking the first numeric range in the text.

### Microsoft internship-signal and summary-card cleanup
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so employment type only marks `internship` for actual role-type phrasing or internship titles, instead of treating any qualification mention like `at least one internship or prior role` as proof that the job itself is an internship.
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so the jobs-page stat cards now show `No matches` instead of `Sources in view`, which keeps the summary consistent with tables that still include `NO` jobs under the current filters.

### Re-score scope and stale signal fix
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so `Batch Re-score Jobs` now operates on the current filtered jobs in the UI instead of an arbitrary top-N global DB batch.
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so opening a job detail page always refreshes saved job intelligence before rendering, which helps stale pills like outdated employment-type signals self-correct.

### Employment type false-positive fix
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) so internship detection now uses word-boundary matches like `intern`, `internship`, and `co-op` instead of a raw substring check that could misclassify text like `internal pay parity` as an internship.

### Ops polish and audit hardening
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) so the jobs page cursor display now uses the real board inventory count instead of a hardcoded total.
- Refreshed [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) `/boards` and `/health` pages with clearer summary stats and scrollable tables so they match the friendlier jobs-page layout better.
- Updated [tools/fortune500_audit.py](/c:/Users/vasis/Desktop/job/job-radar/tools/fortune500_audit.py) to distinguish `board-active`, `board-degraded`, `board-listed`, `main-source`, and alias-based variants instead of collapsing everything into a single covered bucket.
- Updated [src/notifier.py](/c:/Users/vasis/Desktop/job/job-radar/src/notifier.py) so pure source-alert emails are logged as alerts instead of fake `0 yes + 0 maybe` sends, and truly empty notifications are skipped.

### Friendlier jobs dashboard
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) to make the jobs page easier to scan: added a friendlier top section, quick summary stats, clearer scanner explanations, current board cursor display, and a horizontally scrollable jobs table on smaller screens.

### LinkedIn verified badge
- Updated [src/sources/linkedin.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/linkedin.py) to detect a LinkedIn `Verified` / `Verified employer` badge from the job card or detail page and attach it as structured metadata.
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) to merge source-provided structured metadata with the normal extracted signals so source-specific flags like LinkedIn verification are not overwritten during evaluation.
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) to show a `Verified` pill when a LinkedIn job carries that badge.

### Fortune 500 coverage groundwork
- Added [src/company_aliases.py](/c:/Users/vasis/Desktop/job/job-radar/src/company_aliases.py) with a reusable alias-normalization layer for Fortune 500 naming variants such as `Meta Platforms` -> `Meta`, `Capital One Financial` -> `Capital One`, `Walt Disney` -> `Disney`, and `Jones Lang LaSalle` -> `JLL`.
- The alias normalizer also strips generic company suffixes like `Inc.`, `Corporation`, `Group`, and `Co.` before comparison so the audit does not undercount obvious matches.
- Added [tools/fortune500_audit.py](/c:/Users/vasis/Desktop/job/job-radar/tools/fortune500_audit.py) to generate a repeatable covered / alias-covered / missing Fortune 500 audit report into `state/fortune500_audit.csv`.
- Added verified Fortune 500 Workday boards for `Cisco Systems`, `Coca-Cola`, `Eli Lilly`, `General Motors`, and `Johnson & Johnson` to [data/boards/JOB_BOARDS_PURE_WORKING_SUPPORTED_round2.csv](/c:/Users/vasis/Desktop/job/job-radar/data/boards/JOB_BOARDS_PURE_WORKING_SUPPORTED_round2.csv).

### Scan UX cleanup
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) to use clearer scan labels and explain that full board sweeps resume from the saved cursor and wrap until all boards are covered.
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) so live terminal progress bars are disabled for web-triggered scans and kept for direct terminal runs only.
- Zero-new-job scans now keep the existing `No new matching jobs.` log without sending noisy empty-result alerts from normal scan flow.

### Workday heuristic tightening
- Updated [src/sources/workday.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/workday.py) to remove the blanket `architect` skip and replace it with more specific non-target architect variants.
- Added a second-chance Workday detail-enrichment path for titles containing data/analytics/ML/BI/scientist-style markers even when the title classifier does not initially label the role as `yes` or `maybe`.

### Live terminal progress bar
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) so local boards-mode runs render a single live terminal progress bar that advances when each board finishes, instead of only showing batch-boundary jumps.

### Per-board sweep progress
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) so boards-mode progress can advance one board at a time within a batch, using human-style counts like `851/1281`, `852/1281`, rather than only jumping at batch boundaries.

### Boards progress logging
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) so boards-mode terminal logs now show absolute sweep progress and percent complete for each batch, not just the raw `[start:end]` cursor window.

### Workable JD fix and runtime trim
- Updated [src/sources/workable.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/workable.py) to request Workable widget payloads with `details=true` and merge richer description fields instead of relying on the shallow listing shape.
- Added structured Workable location extraction so dict-based location payloads no longer collapse to empty or weak strings.
- Updated [src/sources/smartrecruiters.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/smartrecruiters.py) so SmartRecruiters only fetches expensive detail pages for likely relevant titles instead of every posting on the board.
- Updated [src/sources/workday.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/workday.py) to cap extra non-relevant shallow-detail enrichments per board while preserving full detail fetches for `yes` / `maybe` roles.

### Workday JD coverage stabilization
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) to rehydrate shallow job descriptions from the local DB before run-history and health metrics are recorded.
- This specifically improves repeat-run JD coverage for Workday boards that return shallow listing payloads on refresh even when full descriptions already exist for the same keys locally.

### Workday unresolved metadata rows
- Updated [src/sources/workday.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/workday.py) to normalize more Workday req ID formats including `JR...`, `RQ...`, `P...`, plain numeric IDs, and suffixed variants like `26952591-1`.
- Expanded Workday detail enrichment so rows with short or metadata-only descriptions are forced through detail-page extraction instead of relying on a small capped sample.
- Added a few broader Workday detail selectors to improve extraction when the page structure differs from earlier tenants.

### Workday runtime trim
- Updated [src/sources/workday.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/workday.py) to skip expensive Workday detail fetches for obvious junk such as warehouse / fulfillment / retail / software-engineering rows and some clearly non-US locations.
- This keeps aggressive detail enrichment for potentially relevant data roles while reducing the worst batch-time spikes introduced by the broader Workday parser pass.

### Profile stack sync
- Rewrote [src/profile.py](/c:/Users/vasis/Desktop/job/job-radar/src/profile.py) to match the current job-search brief.
- Updated the default profile template and expanded the scoring stack to match the active search profile.
- Expanded the strong skill stack to include confirmed tools such as `OpenAI API`, `MCP`, `RAG`, `FAISS`, `ChromaDB`, `FastAPI`, `Kafka`, `Airflow`, `dbt`, `GitHub Actions`, `Kubernetes`, `MongoDB`, `QuickSight`, `Google Analytics`, `MLflow`, and monitoring concepts.
- Moved lighter tools like `Looker`, `Alteryx`, `Hive`, and `HQL` into the moderate stack.
- Removed stale / weaker leftovers from the scoring profile.

### Scoring and filtering hardening
- Updated [src/evaluation.py](/c:/Users/vasis/Desktop/job/job-radar/src/evaluation.py) so:
  - `4+ years` is a hard block.
  - titles like `Principal`, `Staff`, `Senior Staff`, `Distinguished`, and `Fellow` are hard-blocked.
  - non-US jobs are blocked in scoring when `require_us_location` is enabled.
  - weak or missing JD text caps weak sources so empty-description jobs do not float up as strong matches.
- Updated [src/main.py](/c:/Users/vasis/Desktop/job/job-radar/src/main.py) and [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) to pass source and US-only config into evaluation consistently.

### Workday and UI quality-of-life cleanup
- Updated [src/webapp.py](/c:/Users/vasis/Desktop/job/job-radar/src/webapp.py) to:
  - hide legacy Workday `url:` duplicates when canonical req-based rows exist,
  - redirect hidden duplicate detail pages to the canonical row,
  - show hidden-item notes,
  - add a `No JD` signal pill.
- Fixed batch re-score flow so it refreshes job intelligence before re-evaluating rows.

### Text normalization and duplicate helpers
- Updated [src/job_intelligence.py](/c:/Users/vasis/Desktop/job/job-radar/src/job_intelligence.py) with:
  - Workday req ID extraction,
  - mojibake cleanup such as `Youâ€™ll` -> `You'll`.
- Updated [src/database.py](/c:/Users/vasis/Desktop/job/job-radar/src/database.py) to normalize stored descriptions during save / refresh paths and expose Workday duplicate-key helpers.

### Location parsing fix
- Updated [src/sources/base.py](/c:/Users/vasis/Desktop/job/job-radar/src/sources/base.py) so locations like `Buenos Aires, Buenos Aires, ar` are no longer misread as US state abbreviations.

### Verification
- Confirmed imports for `src.job_intelligence`, `src.sources.base`, `src.evaluation`, `src.database`, `src.webapp`, and `src.main`.
- Verified examples:
  - mojibake normalization returns `You'll ...`
  - `Buenos Aires, Buenos Aires, ar` now scores `NO / F`
  - `Senior ... 8+ years` now scores `NO / F`
