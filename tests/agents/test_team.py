"""`build_team`'s structural wiring (leader mode, guardrails, member tool
sets, output schema) and the citation post-hook that enforces "every number
in the answer must appear in a cited cube row" independent of prose parsing.
"""

import json
from types import SimpleNamespace

import pytest
from agno.team.mode import TeamMode

from fpa_be.agents.model import default_model
from fpa_be.agents.team import (
    DRIFT_DETECTOR_MEMBER_ID,
    TOOL_CALL_LIMIT,
    Citation,
    CopilotAnswer,
    UncitedNumberError,
    _numbers_in,
    build_team,
    citation_post_hook,
    drift_post_hook,
    executed_queries,
)
from fpa_be.masking.gate import disclosure_tool_hook


def _tool_execution(tool_name, result):
    return SimpleNamespace(tool_name=tool_name, result=result)


def _member_response(agent_id, tool_results):
    return SimpleNamespace(agent_id=agent_id, tools=[_tool_execution("run_finops_query", r) for r in tool_results])


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

    def test_every_member_carries_the_masking_gate_innermost(self, team):
        """Agno nests tool hooks first-outermost: the masking gate must be
        last, so it wraps the tool directly and runs on every tool call."""
        for member in team.members:
            assert len(member.tool_hooks) == 2
            assert member.tool_hooks[-1] is disclosure_tool_hook

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


_DSL = "SELECT services_revenue WHERE geo_country = 'PL' FOR PERIOD 2026-Q2"
_RESULT = json.dumps({"dsl": _DSL, "vintage": None, "columns": ["services_revenue"], "rows": [[41431076.19]]})


def _ran(answer, results=(_RESULT,)):
    return SimpleNamespace(content=answer, member_responses=[_member_response("query-answerer", list(results))])


class TestCitationPostHook:
    def test_passes_when_every_number_is_cited_from_a_query_that_ran(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 41431076.19.",
            citations=[Citation(dsl=_DSL, row=["41431076.19"])],
        )
        citation_post_hook(run_output=_ran(answer))

    def test_raises_when_a_number_has_no_citation(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 999999999.99.",
            citations=[Citation(dsl=_DSL, row=["41431076.19"])],
        )
        with pytest.raises(UncitedNumberError):
            citation_post_hook(run_output=_ran(answer))

    def test_raises_when_the_citation_names_a_query_no_member_ran(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 41431076.19.",
            citations=[Citation(dsl="SELECT services_revenue FOR PERIOD 2026-Q3", row=["41431076.19"])],
        )
        with pytest.raises(UncitedNumberError, match="no member ran"):
            citation_post_hook(run_output=_ran(answer))

    def test_raises_when_the_model_invents_the_cited_row_itself(self):
        answer = CopilotAnswer(
            answer="Poland Q2 revenue was 999999999.99.",
            citations=[Citation(dsl=_DSL, row=["999999999.99"])],
        )
        with pytest.raises(UncitedNumberError, match="was not returned"):
            citation_post_hook(run_output=_ran(answer))

    def test_executed_queries_carry_member_id_dsl_and_vintage(self):
        [query] = executed_queries(_ran(CopilotAnswer(answer="")))
        assert (query["member_id"], query["dsl"], query["vintage"]) == ("query-answerer", _DSL, None)

    def test_ignores_non_copilot_answer_content(self):
        citation_post_hook(run_output=SimpleNamespace(content="plain string, not a CopilotAnswer"))

    def test_ignores_missing_run_output(self):
        citation_post_hook(run_output=None)


class TestDriftPostHook:
    def test_forces_drift_flag_when_member_totals_disagree(self):
        answer = CopilotAnswer(answer="No drift detected.", drift_flag=False)
        member_response = _member_response(
            DRIFT_DETECTOR_MEMBER_ID,
            [
                json.dumps({"rows": [["100.00"]]}),
                json.dumps({"rows": [["200.00"]]}),
            ],
        )
        drift_post_hook(run_output=SimpleNamespace(content=answer, member_responses=[member_response]))
        assert answer.drift_flag is True

    def test_leaves_drift_flag_false_when_totals_agree(self):
        answer = CopilotAnswer(answer="No drift detected.", drift_flag=False)
        member_response = _member_response(
            DRIFT_DETECTOR_MEMBER_ID,
            [
                json.dumps({"rows": [["100.00"]]}),
                json.dumps({"rows": [["100.00"]]}),
            ],
        )
        drift_post_hook(run_output=SimpleNamespace(content=answer, member_responses=[member_response]))
        assert answer.drift_flag is False

    def test_ignores_non_drift_detector_members(self):
        answer = CopilotAnswer(answer="answer", drift_flag=False)
        member_response = _member_response(
            "query-answerer",
            [json.dumps({"rows": [["100.00"]]}), json.dumps({"rows": [["999.00"]]})],
        )
        drift_post_hook(run_output=SimpleNamespace(content=answer, member_responses=[member_response]))
        assert answer.drift_flag is False

    def test_ignores_non_copilot_answer_content(self):
        drift_post_hook(run_output=SimpleNamespace(content="plain string", member_responses=[]))

    def test_ignores_missing_run_output(self):
        drift_post_hook(run_output=None)
