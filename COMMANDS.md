# Job Radar Commands

## PowerShell setup

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Main commands

```powershell
python -m src.main --mode web
python -m src.main
python -m src.main --mode boards
python -m src.main --test-notify
python -m src.main --health-check
```

## Operating model

- `public-export` is the public GitHub Actions repo and the remote automation source of truth.
- `job-radar` is the local development repo.
- Public GitHub board sweeps run every 2 hours with no cooldown.
- Local dashboard launches can still compare `public-export/state/gha-jobs.db` with the sibling `job-radar` DB and surface which one is active.

## Useful variants

```powershell
python -u -m src.main --mode web
python -m src.main --dry-run --verbose
python -m src.main --mode boards --dry-run --verbose
python -m src.main --mode boards --boards-batch-size 50
python -m src.main --mode boards --boards-run-until-wrap
python -m src.main --mode web --web-port 8080
```

## Open the web UI

```text
http://127.0.0.1:8080
```

## Base resume file

Edit this file to change the base resume used for generated drafts:

```text
data/resume/base_resume.md
```

## Config checks

```powershell
python -m src.main --test-notify
python -m src.main --mode web
```

## If dependencies are missing

```powershell
python -m pip install -r requirements.txt
```
