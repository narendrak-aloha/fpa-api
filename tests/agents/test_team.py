"""`build_team`'s structural wiring (leader mode, guardrails, member tool
sets, output schema) and the citation post-hook that enforces "every number
in the answer must appear in a cited cube row" independent of prose parsing.
"""

from types import SimpleNamespace

import pytest
from agno.team.mode import TeamMode

from fpa_be.agents.model import default_model
from fpa_be.agents.team import (
    TOOL_CALL_LIMIT,
    Citation,
    CopilotAnswer,
    UncitedNumberError,
    _numbers_in,
    build_team,
    citation_post_hook,
)


@pytest.fixture(scope="module")
def team():
    return build_team(default_model())


class TestTeamStructure:
    def test_coordinate_mode(self, team):
        assert team.mode == TeamMode.coordinate

    def test_four_members(self, team):
        member_names = {m.name for m in team.members}
        assert member_names == {"query-answerer", "gap-explainer", "driver-proposer", "drift-detector"}

    def test_member_ids_are_stable_constants(self, team):
        assert {m.id for m in team.members} == {"query-answerer", "gap-explainer", "driver-proposer", "drift-detector"}

    def test_only_driver_proposer_has_propose_driver_tool(self, team):
        for member in team.members:
            tool_names = {t.name for t in member.tools}
            if member.name == "driver-proposer":
                assert "propose_driver" in tool_names
            else:
                assert "propose_driver" not in tool_names

    def test_leader_and_members_carry_shared_guardrails(self, team):
        assert len(team.pre_hooks) == 2
        for member in team.members:
            assert len(member.pre_hooks) == 2

    def test_leader_has_citation_post_hook(self, team):
        assert citation_post_hook in team.post_hooks

    def test_leader_output_schema_is_copilot_answer(self, team):
        assert team.output_schema is CopilotAnswer

    def test_tool_call_limit_applied_to_leader_and_members(self, team):
        assert team.tool_call_limit == TOOL_CALL_LIMIT
        for member in team.members:
            assert member.tool_call_limit == TOOL_CALL_LIMIT


class TestNumbersIn:
    def test_extracts_integers_and_decimals(self):
        assert _numbers_in("revenue was 41431076.19 in PL, up from 40,000,000") == {"41431076.19", "40,000,000"}

    def test_no_numbers_returns_empty_set(self):
        assert _numbers_in("no figures here") == set()

    def test_letter_prefixed_labels_are_not_numbers(self):
        assert _numbers_in("Poland Q2 revenue was 41431076.19.") == {"41431076.19"}


class TestCitationPostHook:
    def test_passes_when_every_number_is_cited(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 41431076.19.",
            citations=[Citation(dsl="SELECT services_revenue", row=["41431076.19"])],
        )
        citation_post_hook(run_output=SimpleNamespace(content=answer))

    def test_raises_when_a_number_has_no_citation(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 999999999.99.",
            citations=[Citation(dsl="SELECT services_revenue", row=["41431076.19"])],
        )
        with pytest.raises(UncitedNumberError):
            citation_post_hook(run_output=SimpleNamespace(content=answer))

    def test_ignores_non_copilot_answer_content(self):
        citation_post_hook(run_output=SimpleNamespace(content="plain string, not a CopilotAnswer"))

    def test_ignores_missing_run_output(self):
        citation_post_hook(run_output=None)
