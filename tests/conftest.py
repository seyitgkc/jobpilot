import pytest

from jobpilot.config import Config, DataPaths
from jobpilot.db import open_db

FIXTURE_FACTS = """\
profile:
  name: Jamie Doe
  headline: Backend Engineer | APIs & Data
  location: Toronto, ON
  phone: "+1 (555) 000-1111"
  email: jamie@example.com
  linkedin: linkedin.com/in/jamiedoe
  github: github.com/jamiedoe
  languages: English (Native) | Turkish (Basic)

facts:
  - id: summary.main
    kind: summary
    text: Backend engineer with 5+ years building APIs in Go and C#, cutting costs 30% at scale.
  - id: skills.languages
    kind: skill_group
    label: Languages
    text: Go, C#, SQL, Python
  - id: skills.infra
    kind: skill_group
    label: Infrastructure
    text: Docker, Kubernetes, AWS
  - id: exp.acme
    kind: experience
    org: Acme Corp
    role: Senior Backend Engineer
    location: Toronto, ON
    dates: May 2021 - Present
  - id: exp.acme.gateway
    kind: bullet
    parent: exp.acme
    text: Built the payments gateway in Go, processing 2M requests per day with 99.9% uptime
  - id: exp.acme.migration
    kind: bullet
    parent: exp.acme
    text: Migrated the billing monolith to microservices in May 2023, cutting deploy time 40%
  - id: exp.globex
    kind: experience
    org: Globex
    role: Backend Engineer
    location: Ankara, Türkiye
    dates: Jan. 2019 - Apr. 2021
  - id: exp.globex.search
    kind: bullet
    parent: exp.globex
    text: Rebuilt product search on PostgreSQL, improving relevance for 87% of queries
  - id: proj.widget
    kind: project
    text: Widget Engine
    label: Go, SQLite
  - id: proj.widget.rules
    kind: bullet
    parent: proj.widget
    text: Built a rules engine processing 10k events per second on a single node
  - id: edu.metu
    kind: education
    org: Middle East Technical University
    role: B.Sc. in Computer Engineering
    location: Ankara, Türkiye
    dates: 2014 - 2018
  - id: edu.metu.coursework
    kind: bullet
    parent: edu.metu
    text: "Relevant coursework: Databases, Networks, Operating Systems"
  - id: cert.cloud
    kind: certification
    text: Cloud Practitioner
    org: Example Institute
    location: Toronto, ON
    dates: Mar. 2020
  - id: cert.cloud.exam
    kind: bullet
    parent: cert.cloud
    text: Passed the practitioner exam on the first attempt
"""


def write_facts(paths, content=FIXTURE_FACTS):
    paths.resume_master_dir.mkdir(parents=True, exist_ok=True)
    (paths.resume_master_dir / "facts.yaml").write_text(content)


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
