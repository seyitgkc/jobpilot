"""Live smoke test against the real Anthropic API. Runs only when
JOBPILOT_LIVE_AI=1 and an API key are set; everything else mocks providers."""

import os

import pytest

from conftest import write_facts
from jobpilot.config import Config
from jobpilot.core import honesty, resume
from jobpilot.providers.anthropic_provider import AnthropicProvider
from jobpilot.providers.schemas import TailorConstraints

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBPILOT_LIVE_AI") != "1"
    or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live provider test needs JOBPILOT_LIVE_AI=1 and ANTHROPIC_API_KEY",
)

POSTING = """# Senior Backend Engineer at Initech

We build payment infrastructure in Go on PostgreSQL. You will own services
end to end, from schema design to deployment. Kubernetes experience helps.
"""


def test_live_tailor_produces_valid_plan(paths):
    write_facts(paths)
    facts = resume.load_facts(paths)
    provider = AnthropicProvider(Config().ai)
    constraints = TailorConstraints(rules=["never use the word synergy"])

    plan = provider.tailor(facts, POSTING, constraints)

    report = honesty.validate_plan(plan, facts, [], constraints.token_overlap_min)
    assert report.ok, report.summary_lines()
    assert plan.experience, "plan selected no experience"
    tex = resume.render_tex(facts, plan, resume.load_template_source(paths))
    assert "\\begin{document}" in tex
