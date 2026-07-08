# LocalOCR Job Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a lightweight local job registry that prevents duplicate OCR submissions and reuses completed OCR outputs for identical file/model/output requests.

**Architecture:** Add a file-backed registry under `_server/jobs` with one JSON manifest and one lock file per job key. `OCRService.process_inputs()` computes a content-hash-based key before inference: completed jobs return cached output files, running jobs return `active_localocr_task`, and new jobs are claimed with an atomic lock before OCR starts.

**Tech Stack:** Python 3.12, stdlib `hashlib/json/os/pathlib/time`, existing FastAPI service, existing PowerShell wrappers, existing unittest suite.

## Global Constraints

- Do not add Redis, SQLite, background worker services, or external dependencies.
- Keep cache local-only and ignored by Git under `_server/jobs`.
- Cache applies only when `write_files=True`; no-output calls keep current uncached behavior.
- Preserve existing `ocr_smart.ps1`, `ocr_once.ps1`, `/ocr/path`, and `/ocr/file` response compatibility.
- Do not make `structure` or `vl` resident in the API process.

---

### Task 1: Job Registry Unit

**Files:**
- Create: `E:\Projects\Tools\LocalOCR\localocr\job_registry.py`
- Create: `E:\Projects\Tools\LocalOCR\tests\test_job_registry.py`

**Interfaces:**
- Produces: `JobRegistry(job_dir: Path)`, `JobRequest`, `JobClaim`, `build_request(...)`, `try_claim(...)`, `complete(...)`, `fail(...)`, `release(...)`.
- Consumes: `ModelProfile` from `localocr.model_registry`.

- [ ] **Step 1: Write failing tests**

Tests must cover:
- identical file content + same profile + same output dir gives the same `job_key`
- changed file content changes `job_key`
- completed manifest with existing output files returns `cache_hit`
- running lock with live or unknown owner returns `active`

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
wsl -d Ubuntu -e bash -lc "cd /mnt/e/Projects/Tools/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_job_registry"
```

Expected: fail because `localocr.job_registry` does not exist.

- [ ] **Step 3: Implement registry**

Implement JSON manifests with schema version, job id/key, source hash, profile id, output dir, status, timestamps, output files, and error tail. Use `os.open(..., O_CREAT | O_EXCL)` for atomic claim locks.

- [ ] **Step 4: Run tests and verify GREEN**

Run the same unittest command. Expected: OK.

### Task 2: Service Integration

**Files:**
- Modify: `E:\Projects\Tools\LocalOCR\localocr\service.py`
- Modify: `E:\Projects\Tools\LocalOCR\localocr\server.py`
- Modify: `E:\Projects\Tools\LocalOCR\tests\test_service_process.py`

**Interfaces:**
- Consumes: `JobRegistry` from Task 1.
- Produces: cached results containing `cache_status=cache_hit`, `job_id`, `job_key`; active duplicate responses containing top-level `status=active_localocr_task`.

- [ ] **Step 1: Write failing service tests**

Tests must instantiate `OCRService(probe_on_start=False, job_dir=<tmp>)` with a fake `process_file()` implementation and assert:
- first request runs OCR and writes outputs
- second identical request returns `cache_hit` and does not call OCR again
- a pre-existing running claim makes `process_inputs()` return `ok=false`, `status=active_localocr_task`

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
wsl -d Ubuntu -e bash -lc "cd /mnt/e/Projects/Tools/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_service_process"
```

Expected: fail because service has no job registry.

- [ ] **Step 3: Integrate registry before inference**

In `process_inputs()`, compute profile and output dir, ask registry for a claim, return cache/active immediately, and only run inference for `run` claims. Mark completion after `write_outputs()` paths are known. Mark failure and release lock on exceptions.

- [ ] **Step 4: Run targeted tests**

Run service and registry tests. Expected: OK.

### Task 3: Smart Wrapper and Docs

**Files:**
- Modify: `E:\Projects\Tools\LocalOCR\ocr_smart.ps1`
- Modify: `E:\Projects\Tools\LocalOCR\README.md`
- Modify: `E:\Projects\Tools\LocalOCR\docs\QUICKSTART_FOR_AI.md`
- Modify: `E:\Projects\Tools\LocalOCR\docs\TROUBLESHOOTING.md`
- Modify: `E:\.agents\skills\localocr\SKILL.md`

**Interfaces:**
- Consumes: service response fields `cache_status`, `job_id`, `job_key`, `status=active_localocr_task`.
- Produces: documented cache-hit and active-job behavior for future AI calls.

- [ ] **Step 1: Write wrapper/documentation assertions**

Extend `tests/test_windows_wrappers.py` to assert smart wrapper preserves `cache_status` and documents `active_localocr_task`.

- [ ] **Step 2: Run tests and verify RED if needed**

Run wrapper tests. Expected: fail until docs/wrapper text is updated.

- [ ] **Step 3: Update docs and wrapper comments**

Document `_server/jobs`, cache hit behavior, running duplicate behavior, and `-Force` bypass semantics.

- [ ] **Step 4: Full verification**

Run:

```powershell
wsl -d Ubuntu -e bash -lc "cd /mnt/e/Projects/Tools/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_job_registry tests.test_service_process tests.test_windows_wrappers"
wsl -d Ubuntu -e bash -lc "cd /mnt/e/Projects/Tools/LocalOCR && scripts/run_in_wsl.sh -m compileall localocr"
```

Then run a real OCR cache smoke:

```powershell
& 'E:\Projects\Tools\LocalOCR\release_resources.ps1' -WslTimeoutSec 10
& 'E:\Projects\Tools\LocalOCR\ocr_smart.ps1' 'E:\Projects\Tools\LocalOCR\tests\samples\probe_text.png' -Engine ocr -OuterTimeoutSec 180 -StartupTimeoutSec 900 -StopAfter -Force
& 'E:\Projects\Tools\LocalOCR\ocr_smart.ps1' 'E:\Projects\Tools\LocalOCR\tests\samples\probe_text.png' -Engine ocr -OuterTimeoutSec 60 -StartupTimeoutSec 900 -StopAfter
```

Expected: first call runs OCR, second call returns `cache_status=cache_hit` without loading OCR.
