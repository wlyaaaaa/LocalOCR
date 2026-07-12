# LocalOCR Readiness Wrapper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LocalOCR readiness triage zero-path and eliminate null-exit-code false failures.

**Architecture:** Keep the existing wrapper and JSON contract. Add input branching before route preview, finalize bounded child state before reading `ExitCode`, and cover both behaviors with lightweight wrapper tests.

**Tech Stack:** PowerShell 7.6, Python `unittest`.

## Global Constraints

- Do not start OCR inference or load a GPU model during implementation or verification.
- Do not change ports, model profiles, Broker behavior, or global Agent rules.
- Preserve existing path-based triage output.

---

### Task 1: Add failing readiness and process-finalization tests

**Files:**
- Modify: `tests/test_windows_wrappers.py`

**Interfaces:**
- Consumes: `ocr_smart.ps1 -TriageOnly`
- Produces: regression coverage for zero-path JSON and finalized exit-code handling

- [x] **Step 1: Write a test that runs zero-path triage and asserts `ok=true`, `status=triage_only`, and `route_reason=not_applicable_without_path`.**
- [x] **Step 2: Add source-contract assertions for parameterless `WaitForExit()`, `Refresh()`, and `$ProgressPreference = 'SilentlyContinue'`.**
- [x] **Step 3: Run `python -m unittest tests.test_windows_wrappers -v` and confirm the new tests fail for the missing behavior.**

### Task 2: Implement the minimal wrapper changes

**Files:**
- Modify: `ocr_smart.ps1`

**Interfaces:**
- Consumes: optional `Path`, `TriageOnly`, child process completion
- Produces: compact triage/missing-path JSON and reliable child exit status

- [x] **Step 1: Remove mandatory binding from `Path` and branch zero-path triage before route preview.**
- [x] **Step 2: Return `missing_path` JSON for normal invocation without a path.**
- [x] **Step 3: Finalize and refresh completed child processes before reading `ExitCode`; suppress child progress output.**
- [x] **Step 4: Re-run `python -m unittest tests.test_windows_wrappers -v` and confirm all tests pass.**

### Task 3: Verify and close out

**Files:**
- Verify: `ocr_smart.ps1`, `tests/test_windows_wrappers.py`

**Interfaces:**
- Consumes: completed implementation
- Produces: clean feature-branch commit and synchronized remote

- [x] **Step 1: Run the full lightweight unit suite and `git diff --check`.**
- [x] **Step 2: Run zero-path triage once and confirm it does not create OCR outputs or load an engine.**
- [ ] **Step 3: Scan the public diff for secrets, stage explicit files, commit, and push the feature branch.**
