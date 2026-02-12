from __future__ import annotations

import base64
import io
from pathlib import Path


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    keep = max_chars - 64
    return f"{text[:keep]}\n\n[内容已截断，原始长度 {len(text)} 字符]"


def _read_plain_text(path: Path, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _truncate(text, max_chars)


def _extract_pdf(path: Path, max_chars: int) -> str:
    from pypdf import PdfReader  # lazy import

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for idx, page in enumerate(reader.pages, start=1):
        chunks.append(f"\n--- Page {idx} ---\n")
        chunks.append(page.extract_text() or "")
        if sum(len(c) for c in chunks) > max_chars:
            break
    return _truncate("".join(chunks), max_chars)


def _extract_docx(path: Path, max_chars: int) -> str:
    from docx import Document  # lazy import

    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return _truncate(text, max_chars)


def extract_document_text(path: str, max_chars: int) -> str | None:
    file_path = Path(path)
    suffix = file_path.suffix.lower()

    plain_suffixes = {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".html",
        ".css",
        ".yaml",
        ".yml",
        ".xml",
    }

    try:
        if suffix in plain_suffixes:
            return _read_plain_text(file_path, max_chars)
        if suffix == ".pdf":
            return _extract_pdf(file_path, max_chars)
        if suffix == ".docx":
            return _extract_docx(file_path, max_chars)
    except Exception as exc:
        return f"[文档解析失败: {exc}]"

    return None


def _heic_to_jpeg_bytes(path: Path) -> bytes:
    try:
        from PIL import Image
        from pillow_heif import register_heif_opener

        register_heif_opener()
        image = Image.open(path)
        rgb = image.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=92)
        return buffer.getvalue()
    except Exception as exc:
        raise RuntimeError(
            "HEIC/HEIF conversion requires pillow-heif. Please install dependencies from requirements.txt."
        ) from exc


def image_to_data_url(path: str, mime: str) -> str:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    raw: bytes
    out_mime = mime

    if suffix in {".heic", ".heif"} or mime in {"image/heic", "image/heif"}:
        raw = _heic_to_jpeg_bytes(file_path)
        out_mime = "image/jpeg"
    else:
        raw = file_path.read_bytes()

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{out_mime};base64,{encoded}"
