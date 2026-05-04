"""
Optional PDF ingestion: extract text with pdfplumber, best-effort Q/A split.

Use from CLI: --pdf-dir path [--pdf-source-name "My PDF Archive"]
"""

from __future__ import annotations

import logging
from pathlib import Path

from mufti_scraper.cleaning import canonical_url, normalize_text
from mufti_scraper.sources.base import ParsedFatwa

logger = logging.getLogger(__name__)


def iter_fatwas_from_pdf_dir(
    directory: Path,
    source_name: str,
    base_url: str = "file://pdf",
) -> list[ParsedFatwa]:
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber is required for PDF extraction. pip install pdfplumber") from e

    out: list[ParsedFatwa] = []
    for path in sorted(directory.rglob("*.pdf")):
        try:
            text_parts: list[str] = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    if t.strip():
                        text_parts.append(t)
            raw = normalize_text("\n\n".join(text_parts))
            if len(raw) < 40:
                continue
            question, answer = _split_q_a(raw)
            url = canonical_url(f"{base_url}/{path.name}")
            out.append(
                ParsedFatwa(
                    question=question,
                    answer=answer,
                    source=source_name,
                    url=url,
                    category="pdf",
                    date=None,
                )
            )
        except Exception as e:
            logger.warning("PDF failed %s: %s", path, e)
    return out


def _split_q_a(text: str) -> tuple[str, str]:
    if "جواب" in text:
        idx = text.index("جواب")
        q, a = text[:idx].strip(), text[idx:].strip()
        return normalize_text(q[:1200]), normalize_text(a)
    if "\n\n" in text:
        head, tail = text.split("\n\n", 1)
        return normalize_text(head[:800]), normalize_text(tail)
    line = text.split("\n", 1)
    q = line[0][:500]
    a = line[1] if len(line) > 1 else text
    return normalize_text(q), normalize_text(a)
