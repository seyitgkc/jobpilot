import pytest

from conftest import write_facts
from jobpilot.core import latex, resume


@pytest.fixture
def facts(paths):
    write_facts(paths)
    return resume.load_facts(paths)


def test_load_facts_roundtrip(facts):
    assert facts.profile.name == "Jamie Doe"
    assert facts.by_id("exp.acme.gateway").parent == "exp.acme"
    assert len(facts.of_kind("experience")) == 2
    assert [b.id for b in facts.bullets_of("exp.acme")] == [
        "exp.acme.gateway",
        "exp.acme.migration",
    ]


def test_load_facts_missing_file(paths):
    with pytest.raises(resume.ResumeSetupError):
        resume.load_facts(paths)


@pytest.mark.parametrize(
    "broken",
    [
        # duplicate id
        "profile: {name: X}\nfacts:\n"
        "  - {id: a, kind: project, text: P}\n  - {id: a, kind: project, text: Q}\n",
        # bullet without parent
        "profile: {name: X}\nfacts:\n  - {id: b, kind: bullet, text: T}\n",
        # bullet parent cannot be a skill group
        "profile: {name: X}\nfacts:\n"
        "  - {id: s, kind: skill_group, label: L, text: Go}\n"
        "  - {id: b, kind: bullet, parent: s, text: T}\n",
        # skill group without label
        "profile: {name: X}\nfacts:\n  - {id: s, kind: skill_group, text: Go}\n",
        # experience without dates
        "profile: {name: X}\nfacts:\n"
        "  - {id: e, kind: experience, org: A, role: R}\n",
    ],
)
def test_load_facts_rejects_broken(paths, broken):
    write_facts(paths, broken)
    with pytest.raises(resume.ResumeSetupError):
        resume.load_facts(paths)


def test_tex_escape():
    assert resume.tex_escape("C# & 95%_done {x^2}") == (
        r"C\# \& 95\%\_done \{x\textasciicircum{}2\}"
    )
    assert resume.tex_escape("a | b") == r"a $|$ b"
    assert resume.tex_escape("Türkiye") == "Türkiye"


def test_render_full_plan(facts, paths):
    plan = resume.full_plan(facts)
    tex = resume.render_tex(facts, plan, resume.load_template_source(paths))
    assert r"\textbf{\Huge \scshape Jamie Doe}" in tex
    assert r"Backend Engineer $|$ APIs \& Data" in tex
    assert r"\href{mailto:jamie@example.com}" in tex
    assert r"{Senior Backend Engineer}{May 2021 -- Present}" in tex
    assert r"{Acme Corp}{Toronto, ON}" in tex
    assert "99.9\\% uptime" in tex
    assert r"\textbf{Languages}{: Go, C\#, SQL, Python}" in tex
    assert r"{\textbf{Widget Engine} $|$ \emph{Go, SQLite}}{}" in tex
    assert "rules engine processing 10k events" in tex
    assert "Middle East Technical University" in tex
    assert "Relevant coursework: Databases, Networks, Operating Systems" in tex
    assert "Cloud Practitioner" in tex
    assert "Passed the practitioner exam" in tex
    assert "English (Native) $|$ Turkish (Basic)" in tex
    assert "Türkiye" in tex
    assert r"\needspace{3\baselineskip}" in tex


def test_render_respects_section_order_and_drops(facts, paths):
    plan = resume.full_plan(facts)
    plan = plan.model_copy(
        update={"section_order": ["experience", "skills"], "projects": []}
    )
    tex = resume.render_tex(facts, plan, resume.load_template_source(paths))
    assert r"\section{Experience}" in tex
    assert tex.index(r"\section{Experience}") < tex.index(r"\section{Technical Skills}")
    assert r"\section{Projects}" not in tex
    assert r"\section{Summary}" not in tex
    assert r"\section{Education}" not in tex


def test_template_override_wins(facts, paths):
    (paths.resume_master_dir / "template.tex.j2").write_text(
        "\\VAR{name} OVERRIDE"
    )
    tex = resume.render_tex(
        facts, resume.full_plan(facts), resume.load_template_source(paths)
    )
    assert tex == "Jamie Doe OVERRIDE"


def test_plan_diff_reports_drops_and_edits(facts):
    plan = resume.full_plan(facts)
    acme = plan.experience[0]
    edited = acme.bullets[0].model_copy(
        update={"rendered_text": "Built the payments gateway in Go"}
    )
    plan = plan.model_copy(
        update={
            "experience": [acme.model_copy(update={"bullets": [edited]})],
            "skills": plan.skills[:1],
            "projects": [],
        }
    )
    diff = resume.plan_diff(facts, plan)

    acme_diff = next(e for e in diff["experiences"] if e["fact_id"] == "exp.acme")
    assert acme_diff["included"]
    assert acme_diff["dropped_bullets"] == ["exp.acme.migration"]
    assert acme_diff["bullets"][0]["changed"]

    globex_diff = next(e for e in diff["experiences"] if e["fact_id"] == "exp.globex")
    assert not globex_diff["included"]
    assert globex_diff["dropped_bullets"] == ["exp.globex.search"]

    assert diff["skills"]["dropped"] == ["skills.infra"]
    widget_diff = next(e for e in diff["projects"] if e["fact_id"] == "proj.widget")
    assert not widget_diff["included"]
    assert widget_diff["name"] == "Widget Engine"
    assert widget_diff["dropped_bullets"] == ["proj.widget.rules"]
    assert diff["summary"]["master"] == diff["summary"]["rendered"]


def test_plan_diff_detects_reorder(facts):
    plan = resume.full_plan(facts)
    acme = plan.experience[0]
    reordered = acme.model_copy(update={"bullets": list(reversed(acme.bullets))})
    plan = plan.model_copy(update={"experience": [reordered, plan.experience[1]]})
    diff = resume.plan_diff(facts, plan)
    acme_diff = next(e for e in diff["experiences"] if e["fact_id"] == "exp.acme")
    assert acme_diff["order_changed"]


@pytest.mark.skipif(not latex.latexmk_available(), reason="latexmk not installed")
def test_compile_full_resume(facts, paths, tmp_path):
    tex = resume.render_tex(
        facts, resume.full_plan(facts), resume.load_template_source(paths)
    )
    tex_path = tmp_path / "resume.tex"
    tex_path.write_text(tex)
    pdf_path = latex.compile_tex(tex_path)
    assert pdf_path.read_bytes()[:5] == b"%PDF-"
