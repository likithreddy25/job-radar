# MASTER RESUME PROMPT — Candidate
## Attach: cv_master.md + RESUME_TEMPLATE.tex + paste JD at bottom
## Works on Claude, Gemini, GPT — identical output every time

---

## ENFORCEMENT CONTRACT

Before doing anything else, acknowledge this contract by writing:
"CONTRACT ACKNOWLEDGED. Starting Step 1."

Then follow every rule below without exception.

WHAT STRICTLY ENFORCED MEANS IN THIS PROMPT:
- Every step has a required output format. Produce that exact format or the step is not done.
- Every step has a gate at the end. You cannot proceed until the gate is passed.
- Every gate requires you to write `GATE PASSED: Step N complete.` before moving on.
- If you skip a step, summarize instead of doing it, or claim it is complete without the actual output, the output is invalid.

WHAT THE AI MUST NEVER DO:
- Never skip a step because it seems obvious.
- Never summarize a step instead of executing it.
- Never move to the next step without writing the gate statement.
- Never deliver files before the human has seen and confirmed the previews.
- Never call the page density acceptable if there is visible empty space at the bottom.
- Never inflate job titles.
- Never let skills live only in the skills section if they are required by the JD and can be defended in experience bullets.

---

## INPUTS

- `cv_master.md` — attached. Full master profile. Read it fully before writing anything.
- `RESUME_TEMPLATE.tex` — attached. Use this template exactly. Never change formatting, margins, font size, header structure, or section commands.
- Job description — pasted at bottom of this prompt.

---

## PERSONA — NEVER SHIFT THIS

Data Scientist / Machine Learning Engineer who also works across analytics and data engineering.
Not an ML researcher. Not a pure BI analyst.
The engineer who makes data and AI systems useful, reliable, and production-ready.
Python and SQL are primary. Spark, dbt, Airflow, cloud, BI, and GenAI stack are used when the JD calls for them.
Early-career data / machine learning engineer with production analytics and platform experience.

---

## STEP 1 — JD ANALYSIS

Extract and list explicitly:
- Role archetype: exactly what type of engineer / analyst / scientist this is day-to-day
- Required skills: every technology explicitly required
- Nice-to-have skills
- Seniority signals
- ATS keywords: exact strings from the JD
- Typical week: what this person actually does Monday through Friday

GATE 1: Write `GATE PASSED: Step 1 complete — JD analysis produced.`

---

## STEP 2 — PROFILE SHAPE AUDIT

Answer all four questions explicitly:
1. What type of role is this day-to-day?
2. What does the current cv_master read as for this role?
3. Where exactly is the shape gap?
4. Which bullets close the gap? Which bullets widen it and must be cut?

Hard rules:
- Wrong-archetype bullets must be cut even if technically impressive.
- Titles stay true; bullets do the tailoring.
- Product / growth bullets should not dominate a platform-heavy role unless the JD values them.
- GenAI bullets should not dominate a BI role unless the JD values them.

GATE 2: Write `GATE PASSED: Step 2 complete — shape audit produced.`

---

## STEP 3 — BULLET SELECTION

Rules:
- 3 bullets per role maximum
- Every required JD skill must appear in an experience bullet when defensible
- First bullet in every role must hit the most JD-relevant skill for that role
- Every bullet must contain: action + technical context + scale or constraint + result
- Every metric must be credible
- No em-dashes; use `--`
- No banned words: leveraged, spearheaded, utilized, championed, passionate about
- Write like an engineer, not a resume writer

GATE 3: Write `GATE PASSED: Step 3 complete — bullets selected.`

---

## STEP 4 — PROJECT SELECTION

Always use exactly 2 projects.
Choose projects that prove skills not already proven strongly in work experience.
Use only projects from `cv_master.md`.

GATE 4: Write `GATE PASSED: Step 4 complete — projects selected: [project 1], [project 2].`

---

## STEP 5 — SUMMARY

3-4 lines maximum.
- Line 1: exact archetype
- Line 2: 2-3 most JD-relevant technical skills
- Line 3: strongest 1-2 metrics relevant to the role
- Line 4: optional, only if needed for density

Hard rules:
- No fluff
- First sentence must signal archetype quickly
- Keep it technical and specific

GATE 5: Write `GATE PASSED: Step 5 complete — summary written.`

---

## STEP 6 — FILL THE TEMPLATE

Use `RESUME_TEMPLATE.tex` exactly as provided.
Hard rules:
- Do not change formatting, margins, font size, header structure, or section commands
- Keep `\pdfgentounicode=1`
- Use the correct address/email rule from the template comments
- Let bullets do the tailoring, not fake titles
- Keep claims defensible

GATE 6: Write `GATE PASSED: Step 6 complete — template filled.`

---

## STEP 7 — FOUR LENS REVIEW

Score each lens 1-10 and explain why:
- ATS keyword match
- Recruiter 30-second scan
- Hiring manager technical depth
- Level and role fit

Fix every weakness before proceeding.

GATE 7: Write `GATE PASSED: Step 7 complete — four lens scores: L1=[X] L2=[X] L3=[X] L4=[X].`

---

## STEP 8 — QUALITY LOOPS

Run at least 3 loops.

### Loop 1 — ATS and shape
- Required JD skills in experience bullets by exact name?
- Summary archetype signal correct?
- Any wrong-archetype bullets?
- Any banned words or em-dashes?

### Loop 2 — Depth and quality
- Every bullet has action + context + scale + result?
- Metrics credible?
- First bullet in each role still aligned to the JD?

### Loop 3 — Density and final quality
- Page fully dense after compile?
- Projects still add value?
- Resume still reads naturally?

After each loop, log:
- flaws found
- fixes made
- page density
- four-lens scores

GATE 8: Write `GATE PASSED: Step 8 complete — minimum quality loops done.`

---

## STEP 9 — PAGE DENSITY ENFORCEMENT

This is a hard block.
The last line of content must sit within 0.3 inches of the bottom margin.

If there is empty space:
1. Expand the shortest role bullet
2. Add a role bullet where most relevant
3. Expand the summary with one specific metric or technology
4. Expand the skills section only with defensible technologies

If it spills to two pages:
1. Remove the weakest project
2. Tighten the longest bullets
3. Shorten the summary

GATE 9: Write `GATE PASSED: Step 9 complete — page density FULL.`

---

## STEP 10 — SCORE CARD

Use this exact format:

Company: [name]
Role: [title]
Candidate: [candidate name]

ATS Keyword Match:         [X/10]
Reason: [...]

Profile Shape Match:       [X/10]
Reason: [...]

Technical Depth Alignment: [X/10]
Reason: [...]

Level and Experience Fit:  [X/10]
Reason: [...]

Differentiation:           [X/10]
Reason: [...]

OVERALL FIT SCORE:         [X/10]
[one honest sentence]

REJECTION RISKS:
- [...]
- [...]
- [...]

Run improvement loops until overall fit is at least 8/10 or honestly capped.

GATE 10: Write `GATE PASSED: Step 10 complete — score card produced.`

---

## STEP 11 — CREDIBILITY CHECK

Ask these explicitly:
- Would a senior engineer or hiring manager say this work is real?
- Does every bullet describe a real technical problem and measurable outcome?
- Are the metrics believable?
- Does the resume tell a coherent story for this JD?
- Does anything feel padded or keyword-stuffed?

Hell Yeah Test:
After reading the resume, the reviewer should think: "hell yeah, this person can do this job."

Log the result:
`Hell Yeah Test: PASSED / FAILED -- reason`

GATE 11: Write `GATE PASSED: Step 11 complete — Hell Yeah Test: [PASSED/FAILED].`

---

## STEP 12 — JD MARKDOWN

Deliver the JD in markdown format:

# Job Description — [Company] — [Role Title]
**Date applied:** [date]
**Location:** [location]
**Link:** [if available]

## Required Skills
- [...]

## Nice-to-Have Skills
- [...]

## Role Summary
[2-3 sentences]

## Key ATS Keywords
[comma-separated exact strings]

## Why This Role Was Selected
[one sentence]

## Resume Variant Used
[which bullets / projects were chosen and why]

GATE 12: Write `GATE PASSED: Step 12 complete — JD markdown prepared.`

---

## FINAL DELIVERY RULES

- Resume must be one page
- Encoding must be clean
- Show previews before links
- Wait for human confirmation before download links
- Deliver resume PDF, resume `.tex`, cover letter PDF, and JD markdown
