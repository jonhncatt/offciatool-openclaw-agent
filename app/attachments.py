from __future__ import annotations

import base64
import io
import re
from html import unescape
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


def _html_to_text(html: str) -> str:
    raw = html or ""
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|li|tr|h1|h2|h3|h4|h5|h6|section|article)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = unescape(raw)
    lines: list[str] = []
    for line in raw.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _extract_outlook_msg(path: Path, max_chars: int) -> str:
    try:
        import extract_msg  # lazy import
    except Exception as exc:
        raise RuntimeError(
            "解析 .msg 需要依赖 extract-msg。请执行 `pip install -r requirements.txt` 后重试。"
        ) from exc

    msg = extract_msg.Message(str(path))
    try:
        subject = (msg.subject or "").strip()
        sender = (msg.sender or "").strip()
        to = (msg.to or "").strip()
        cc = (msg.cc or "").strip()
        date = str(msg.date or "").strip()

        body = (msg.body or "").strip()
        if not body:
            html_body = msg.htmlBody
            if isinstance(html_body, bytes):
                html_body = html_body.decode("utf-8", errors="ignore")
            if isinstance(html_body, str) and html_body.strip():
                body = _html_to_text(html_body)

        attachment_lines: list[str] = []
        for idx, att in enumerate(getattr(msg, "attachments", []) or [], start=1):
            name = (
                (getattr(att, "longFilename", None) or "")
                or (getattr(att, "filename", None) or "")
                or (getattr(att, "name", None) or "")
                or f"attachment_{idx}"
            )
            data = getattr(att, "data", None)
            size = len(data) if isinstance(data, (bytes, bytearray)) else None
            if size is not None:
                attachment_lines.append(f"- {name} ({size} bytes)")
            else:
                attachment_lines.append(f"- {name}")

        sections: list[str] = ["[Outlook MSG 邮件解析]"]
        if subject:
            sections.append(f"主题: {subject}")
        if sender:
            sections.append(f"发件人: {sender}")
        if to:
            sections.append(f"收件人: {to}")
        if cc:
            sections.append(f"抄送: {cc}")
        if date:
            sections.append(f"时间: {date}")
        if attachment_lines:
            sections.append("附件列表:")
            sections.extend(attachment_lines)
        if body:
            sections.append("\n--- 正文 ---\n")
            sections.append(body)

        return _truncate("\n".join(sections).strip(), max_chars)
    finally:
        close = getattr(msg, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


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
        if suffix == ".msg":
            return _extract_outlook_msg(file_path, max_chars)
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


def image_to_data_url_with_meta(path: str, mime: str) -> tuple[str, str | None]:
    """
    Returns (data_url, warning). For HEIC, fallback to original HEIC payload
    when local conversion is unavailable, so capable gateways can still consume it.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    raw: bytes
    out_mime = mime
    warning: str | None = None

    is_heic = suffix in {".heic", ".heif"} or mime in {"image/heic", "image/heif"}
    if is_heic:
        try:
            raw = _heic_to_jpeg_bytes(file_path)
            out_mime = "image/jpeg"
        except Exception:
            raw = file_path.read_bytes()
            out_mime = mime if mime.startswith("image/") else "image/heic"
            warning = "HEIC 未本地转码，已原始上传；若网关不支持 HEIC，请先转 JPG/PNG。"
    else:
        raw = file_path.read_bytes()

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{out_mime};base64,{encoded}", warning


def image_to_data_url(path: str, mime: str) -> str:
    data_url, _ = image_to_data_url_with_meta(path, mime)
    return data_url


def summarize_file_payload(path: str, max_bytes: int = 768, max_text_chars: int = 1200) -> str:
    file_path = Path(path)
    raw = file_path.read_bytes()
    head = raw[:max_bytes]

    if not head:
        return "[空文件]"

    text_bytes = b"\n\r\t\b\f" + bytes(range(32, 127))
    non_text = sum(1 for b in head if b not in text_bytes)
    is_binary = b"\x00" in head or (non_text / len(head)) > 0.30

    if not is_binary:
        text = head.decode("utf-8", errors="ignore")
        text = text[:max_text_chars]
        return f"[文本预览，文件大小 {len(raw)} bytes]\\n{text}"

    hex_preview = " ".join(f"{b:02x}" for b in head[:128])
    return (
        f"[二进制预览，文件大小 {len(raw)} bytes，前 {min(len(head),128)} bytes(hex)]\\n"
        f"{hex_preview}"
    )
