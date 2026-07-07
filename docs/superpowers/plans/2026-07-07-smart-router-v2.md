# Smart Router v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LocalOCR's split `auto` routing with a shared, explainable Smart Router v2 used by Python service/API and the Windows smart wrapper.

**Architecture:** Add a focused Python routing module that returns a route decision with `effective_engine`, `route_reason`, `confidence`, and `signals`. Keep explicit `-Engine` and `-Model` choices authoritative. Service results expose route metadata so wrappers can stay thin and future misroutes are auditable.

**Tech Stack:** Python 3.12, stdlib dataclasses/pathlib/json, existing unittest suite, existing PowerShell wrappers.

## Global Constraints

- No model loading or OCR inference during route selection.
- Do not add LLM arbitration.
- Do not auto-select `structure`; it remains explicit.
- Preserve explicit `-Engine` / `-Model` behavior.
- Route decisions must be deterministic and testable with local files.

---

### Task 1: Shared Smart Router Core

**Files:**
- Create: `localocr/smart_router.py`
- Modify: `localocr/model_registry.py`
- Test: `tests/test_smart_router.py`

**Interfaces:**
- Produces: `SmartRouteDecision` dataclass with `requested_engine`, `requested_model`, `effective_engine`, `route_reason`, `confidence`, `signals`.
- Produces: `choose_smart_route(path: Path, engine_choice: str = "auto", model_choice: str | None = None) -> SmartRouteDecision`.
- Produces: `select_model_profile_with_route(...) -> tuple[ModelProfile, SmartRouteDecision]`.

- [ ] **Step 1: Write failing tests**

Add tests for simple PDF -> OCR, complex PDF filename -> VL, image -> OCR, explicit engine preserved, explicit model explained, and structure never auto-selected.

- [ ] **Step 2: Run tests to verify RED**

Run: `wsl -d Ubuntu -e bash -lc "cd /mnt/e/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_smart_router"`

Expected: failure because `localocr.smart_router` does not exist.

- [ ] **Step 3: Implement minimal router**

Implement deterministic low-cost signals:
- image extensions -> `ocr`, reason `image_prefers_ocr`
- PDF with complex filename keywords (`table`, `formula`, `layout`, `multi`, `论文`, `公式`, `表格`, `多栏`, `课件`) -> `vl`
- other PDF -> `ocr`, reason `pdf_plain_text_prefers_ocr`
- explicit engine -> requested engine, reason `explicit_<engine>`
- explicit model -> model path handled by `model_registry`, reason `explicit_model`

- [ ] **Step 4: Run tests to verify GREEN**

Run the same unittest command. Expected: all tests pass.

### Task 2: Service/API Route Metadata

**Files:**
- Modify: `localocr/service.py`
- Modify: `localocr/server.py`
- Test: `tests/test_service_process.py`

**Interfaces:**
- Service uses `select_model_profile_with_route` in `process_inputs`.
- Each result gets `route`.
- Top-level active response keeps route metadata when the duplicate job is detected.

- [ ] **Step 1: Write failing tests**

Add service tests asserting an auto simple PDF selects OCR route metadata without invoking VL, and cache/active responses preserve route fields.

- [ ] **Step 2: Run tests to verify RED**

Run: `wsl -d Ubuntu -e bash -lc "cd /mnt/e/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_service_process"`

Expected: failure because route metadata is absent.

- [ ] **Step 3: Implement minimal service integration**

Use `select_model_profile_with_route` once per file in `process_inputs`; avoid recomputing a different route inside inference by adding a private helper that accepts the selected profile.

- [ ] **Step 4: Run tests to verify GREEN**

Run the same unittest command. Expected: all tests pass.

### Task 3: Wrapper and Documentation Alignment

**Files:**
- Modify: `ocr_smart.ps1`
- Modify: `tests/test_windows_wrappers.py`
- Modify: `README.md`
- Modify: `docs/QUICKSTART_FOR_AI.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `docs/ARCHITECTURE.md`

**Interfaces:**
- `ocr_smart.ps1` no longer hard-codes PDF -> OCR; it reports requested route fields from the API result.
- `-TriageOnly` still returns a low-token status, with a conservative `effective_engine` preview based on the same keyword rules.

- [ ] **Step 1: Write failing wrapper tests**

Update wrapper tests to expect Smart Router v2 naming and reject the old `simple_pdf_prefers_ocr` hard-coded reason.

- [ ] **Step 2: Run tests to verify RED**

Run: `wsl -d Ubuntu -e bash -lc "cd /mnt/e/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_windows_wrappers"`

Expected: failure until wrapper text/docs are updated.

- [ ] **Step 3: Update wrapper and docs**

Keep wrapper bounded-time behavior, but align reason names with Smart Router v2 and document `route.reason`, `route.signals`, and `route.confidence`.

- [ ] **Step 4: Run focused and smoke tests**

Run:
- `wsl -d Ubuntu -e bash -lc "cd /mnt/e/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_smart_router tests.test_model_registry tests.test_service_process tests.test_windows_wrappers"`
- `E:\LocalOCR\ocr_smart.ps1 "E:\LocalOCR\tests\samples\probe_text.png" -Engine auto -OutDir E:\LocalOCR\_server\smart-router-smoke -OuterTimeoutSec 180 -StartupTimeoutSec 900 -StopAfter`

Expected: tests pass; smoke JSON includes `results[0].route.effective_engine`.
