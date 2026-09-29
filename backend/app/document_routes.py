from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from uuid import uuid4

from docx import Document as WordDocument
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat_routes import _memory_items
from app.database import get_db
from app.models import DocumentMetadata
from app.services.hindsight import HindsightAdapter, get_hindsight_adapter

document_router = APIRouter(prefix="/api/documents", tags=["documents"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EXTRACTED_CHARS = 120_000
MAX_PDF_PAGES = 300
CHUNK_CHARS = 5_000
CHUNK_OVERLAP = 250
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".docx", ".csv"}
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
}


def _metadata(document: DocumentMetadata) -> dict[str, object]:
    return {
        "id": document.id,
        "filename": document.filename,
        "media_type": document.media_type,
        "size_bytes": document.size_bytes,
        "memory_count": document.memory_count,
        "status": document.status,
        "uploaded_at": document.uploaded_at.isoformat() if isinstance(document.uploaded_at, datetime) else None,
    }


def _safe_filename(filename: str | None) -> str:
    # Treat the client filename as a label only. Never use it to construct a path.
    leaf = PurePosixPath((filename or "").replace("\\", "/")).name.strip()
    if not leaf or leaf in {".", ".."} or len(leaf) > 255 or any(ord(char) < 32 for char in leaf):
        raise HTTPException(status_code=400, detail="Choose a valid document filename.")
    extension = PurePosixPath(leaf).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Supported documents are PDF, TXT, DOCX, and CSV.")
    return leaf


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="The text file must use UTF-8 encoding.") from exc


def _extract_text(filename: str, data: bytes) -> str:
    extension = PurePosixPath(filename).suffix.lower()
    if not data:
        raise HTTPException(status_code=422, detail="The selected document is empty.")

    try:
        if extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise HTTPException(status_code=415, detail="The file does not contain a valid PDF document.")
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise HTTPException(status_code=422, detail="Password-protected PDFs are not supported.")
            if len(reader.pages) > MAX_PDF_PAGES:
                raise HTTPException(status_code=413, detail="The PDF contains too many pages to process.")
            parts: list[str] = []
            total = 0
            for page in reader.pages:
                part = page.extract_text() or ""
                if part:
                    remaining = MAX_EXTRACTED_CHARS - total
                    parts.append(part[:remaining])
                    total += min(len(part), remaining)
                    if total >= MAX_EXTRACTED_CHARS:
                        break
            text = "\n\n".join(parts)
        elif extension == ".docx":
            if not zipfile.is_zipfile(io.BytesIO(data)):
                raise HTTPException(status_code=415, detail="The file does not contain a valid DOCX document.")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 5_000 or sum(item.file_size for item in entries) > 30 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="The DOCX expands beyond the processing limit.")
                if "word/document.xml" not in archive.namelist():
                    raise HTTPException(status_code=415, detail="The file does not contain a valid DOCX document.")
            document = WordDocument(io.BytesIO(data))
            parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)[:MAX_EXTRACTED_CHARS]
        elif extension == ".csv":
            decoded = _decode_text(data)
            rows: list[str] = []
            total = 0
            for row in csv.reader(io.StringIO(decoded)):
                line = " | ".join(cell.strip() for cell in row)
                if not line:
                    continue
                remaining = MAX_EXTRACTED_CHARS - total
                rows.append(line[:remaining])
                total += min(len(line), remaining)
                if total >= MAX_EXTRACTED_CHARS:
                    break
                if len(rows) >= 20_000:
                    raise HTTPException(status_code=413, detail="The CSV contains too many rows to process.")
            text = "\n".join(rows)
        else:
            text = _decode_text(data)[:MAX_EXTRACTED_CHARS]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The document is damaged or could not be read.") from exc

    text = re.sub(r"\x00", "", text).strip()
    if not text:
        raise HTTPException(status_code=422, detail="No readable text was found in the document.")
    return text


def _chunks(text: str) -> list[str]:
    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    step = CHUNK_CHARS - CHUNK_OVERLAP
    return [normalized[start:start + CHUNK_CHARS] for start in range(0, len(normalized), step)]


@document_router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> dict[str, object]:
    filename = _safe_filename(file.filename)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Documents must be 10 MB or smaller.")

    extracted_text = _extract_text(filename, data)
    chunks = _chunks(extracted_text)
    document_id = str(uuid4())
    document = DocumentMetadata(
        id=document_id,
        filename=filename,
        media_type=MEDIA_TYPES[PurePosixPath(filename).suffix.lower()],
        size_bytes=len(data),
        memory_count=0,
        status="processing",
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    items = [
        {
            "content": chunk,
            "context": f"Uploaded document: {filename} (part {index + 1} of {len(chunks)})",
            "document_id": document_id,
        }
        for index, chunk in enumerate(chunks)
    ]
    try:
        await hindsight.retain_many(items)
    except Exception as exc:
        document.status = "failed"
        db.commit()
        from app.services.hindsight import HindsightError

        if isinstance(exc, HindsightError):
            raise
        raise HTTPException(status_code=502, detail="The document could not be added to memory.") from exc

    document.memory_count = len(chunks)
    document.status = "ready"
    db.commit()
    db.refresh(document)
    return {"document": _metadata(document), "message": "Document content was added to Hindsight memory."}


@document_router.get("")
def list_documents(db: Session = Depends(get_db)) -> list[dict[str, object]]:
    documents = db.scalars(select(DocumentMetadata).order_by(DocumentMetadata.uploaded_at.desc()))
    return [_metadata(document) for document in documents]


@document_router.get("/{document_id}")
def get_document(document_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    document = db.get(DocumentMetadata, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _metadata(document)


@document_router.get("/{document_id}/memories")
async def recall_document_memories(
    document_id: str,
    db: Session = Depends(get_db),
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
) -> dict[str, object]:
    document = db.get(DocumentMetadata, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    result = await hindsight.recall(
        query=f"Recall useful information from the uploaded document {document.filename}. Document ID: {document.id}",
        types=["experience", "observation", "world"],
        budget="mid",
        max_tokens=4096,
    )
    return {"document": _metadata(document), "memories": [memory.model_dump() for memory in _memory_items(result)]}
