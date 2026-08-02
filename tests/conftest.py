import pytest

from jobpilot.config import Config, DataPaths
from jobpilot.db import open_db


@pytest.fixture
def paths(tmp_path):
    return DataPaths(data_root=tmp_path / "data")


@pytest.fixture
def conn(paths):
    connection = open_db(paths, Config())
    yield connection
    connection.close()


def force_status(conn, job_id, status):
    """Test arrangement only; production code goes through state.transition()."""
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
