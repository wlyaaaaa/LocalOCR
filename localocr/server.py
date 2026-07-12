from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .path_utils import to_wsl_path
from .gpu_broker import GpuBrokerLease
from .service import OCRService


class OCRPathRequest(BaseModel):
    path: str = Field(..., description="Windows or WSL path to image/PDF/folder")
    engine: Literal["auto", "ocr", "vl", "structure"] = "auto"
    model: str | None = Field(None, description="Concrete model profile id; optional")
    recursive: bool = False
    out_dir: str | None = None
    write_outputs: bool = True


class OCRUploadOptions(BaseModel):
    engine: Literal["auto", "ocr", "vl", "structure"] = "auto"
    model: str | None = None
    write_outputs: bool = True


app = FastAPI(
    title="LocalOCR API",
    version="0.5.0",
    description="Local-only OCR API for PP-OCRv6_medium, PaddleOCR-VL-1.6, and PP-StructureV3.",
)

_service: OCRService | None = None


def get_service() -> OCRService:
    global _service
    if _service is None:
        _service = OCRService(
            device="gpu:0",
            tmp_dir="_pdf_pages/api",
            probe_on_start=True,
            gpu_lease_factory=lambda owner: GpuBrokerLease(owner),
        )
    return _service


@app.get("/health")
def health() -> dict:
    service = get_service()
    return {
        "ok": True,
        "gpu": service.gpu_summary,
        "loaded_engines": service.loaded_engines,
        "loaded_models": service.loaded_models,
    }


@app.get("/jobs/{job_key}")
def job_status(job_key: str) -> dict:
    return get_service().job_registry.read_status(job_key)


@app.post("/ocr/path")
def ocr_path(req: OCRPathRequest) -> dict:
    try:
        path = to_wsl_path(req.path)
        out_dir = to_wsl_path(req.out_dir) if req.out_dir else None
        service = get_service()
        return service.process_inputs(
            [path],
            engine_choice=req.engine,
            model_choice=req.model,
            recursive=req.recursive,
            out_dir=out_dir,
            write_files=req.write_outputs,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc


@app.post("/ocr/file")
async def ocr_file(
    file: UploadFile = File(...),
    engine: Literal["auto", "ocr", "vl", "structure"] = "auto",
    model: str | None = None,
    write_outputs: bool = True,
) -> dict:
    suffix = Path(file.filename or "upload").suffix or ".png"
    with tempfile.TemporaryDirectory(prefix="localocr-upload-") as tmp:
        target = Path(tmp) / f"upload{suffix}"
        with target.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            service = get_service()
            return service.process_inputs(
                [target],
                engine_choice=engine,
                model_choice=model,
                recursive=False,
                out_dir=Path("outputs/api_uploads"),
                write_files=write_outputs,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LocalOCR local-only API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("localocr.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
