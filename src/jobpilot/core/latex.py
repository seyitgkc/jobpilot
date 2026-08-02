"""LaTeX compilation via latexmk and pdflatex."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class LatexUnavailable(Exception):
    pass


class CompileError(Exception):
    def __init__(self, message: str, log_excerpt: str):
        super().__init__(message)
        self.log_excerpt = log_excerpt


def latexmk_available() -> bool:
    return shutil.which("latexmk") is not None and shutil.which("pdflatex") is not None


def _log_excerpt(log_path: Path) -> str:
    if not log_path.exists():
        return "(no log file produced)"
    lines = log_path.read_text(errors="replace").splitlines()
    errors: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("!"):
            errors.extend(lines[index : index + 3])
    if errors:
        return "\n".join(errors[:30])
    return "\n".join(lines[-40:])


def compile_tex(tex_path: Path, timeout: int = 180) -> Path:
    """Compile tex_path in place; returns the PDF path. Auxiliary files stay
    in the same directory."""
    if not latexmk_available():
        raise LatexUnavailable(
            "latexmk and pdflatex are required to build the PDF; install a TeX "
            "distribution (BasicTeX is enough) and make sure both are on PATH"
        )
    workdir = tex_path.parent
    result = subprocess.run(
        [
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            tex_path.name,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    pdf_path = tex_path.with_suffix(".pdf")
    if result.returncode != 0 or not pdf_path.exists():
        excerpt = _log_excerpt(tex_path.with_suffix(".log"))
        raise CompileError(f"latexmk failed for {tex_path.name}", excerpt)
    return pdf_path
