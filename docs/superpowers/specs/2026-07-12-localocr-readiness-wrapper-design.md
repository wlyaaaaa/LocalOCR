# LocalOCR Readiness Wrapper Design

## Goal

Make `ocr_smart.ps1 -TriageOnly` a zero-path, non-inference readiness check and prevent completed child processes from being reported as `client_failed` with a null exit code.

## Design

- Make `Path` optional at parameter binding time.
- When `-TriageOnly` is used without `Path`, return health and active-task state with route fields marked not applicable. Do not invoke OCR or create output files.
- When normal mode is used without `Path`, return compact `missing_path` JSON instead of entering OCR execution.
- After a bounded child process exits, call the parameterless `WaitForExit()` and refresh the process before reading `ExitCode`, ensuring redirected streams and exit state are finalized.
- Silence child progress records so successful JSON output is not accompanied by CLIXML progress noise.

## Verification

- A regression test must execute `pwsh -File ocr_smart.ps1 -TriageOnly` without a path and parse successful JSON.
- Wrapper tests must require finalized exit-code handling and progress suppression.
- The full lightweight unit suite must pass; no OCR inference or GPU model load is permitted for acceptance.
