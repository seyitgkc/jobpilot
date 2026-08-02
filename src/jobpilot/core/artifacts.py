"""Artifact storage: bytes onto disk under the job directory, row in the DB.

artifact.path is stored relative to the data root so the tree relocates
cleanly; sha256 pins the exact content a decision was made against.
"""

from __future__ import annotations

import hashlib
import sqlite3

from ..config import DataPaths
from . import repo
from .models import Artifact


def store_bytes(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    kind: str,
    filename: str,
    content: bytes,
    actor: str,
) -> Artifact:
    directory = paths.job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / filename
    file_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    return repo.add_artifact(
        conn, job_id, kind, paths.relative_to_root(file_path), digest, actor
    )


def store_text(
    conn: sqlite3.Connection,
    paths: DataPaths,
    job_id: int,
    kind: str,
    filename: str,
    content: str,
    actor: str,
) -> Artifact:
    return store_bytes(conn, paths, job_id, kind, filename, content.encode(), actor)
