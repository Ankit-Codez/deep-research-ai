"""Incomplete model judgments get one bounded repair, never assumed support."""
import json
from unittest.mock import Mock

from deepresearch_ai.agents.evidence import EvidenceAgent
from deepresearch_ai.agents.verification import VerificationAgent
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import initial_state
from deepresearch_ai.models import ClaimVerification, ResearchRequest
from deepresearch_ai.models.agents import ResearchClaim, VerificationOutput
from deepresearch_ai.models.research import PassageAssessment
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_sources
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.runtime import ResearchRuntime


def extracted():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    state['sources'] = sample_sources()
    state.update(EvidenceAgent(MockChatModel())(state))
    return state


def output(state, *, sparse=False, conflicting=False):
    results = []
    for claim in state['claims']:
        items = [item for item in state['evidence'] if not sparse or item.evidence_id in claim.evidence_ids]
        assessments = [PassageAssessment(evidence_id=item.evidence_id,
            relation=('supports' if item.evidence_id in claim.evidence_ids else
                      'contradicts' if conflicting else 'irrelevant'),
            explanation='A controlled evidence-relative judgment.') for item in items]
        results.append(ClaimVerification(claim_id=claim.claim_id, claim=claim.claim,
            verdict='supported' if claim.evidence_ids else 'insufficient_evidence', rationale='A test assessment.',
            evidence_ids=[item.evidence_id for item in items], passage_assessments=assessments))
    return VerificationOutput(results=results)


def scripted(*responses):
    invoke = Mock(side_effect=responses)
    model = Mock()
    model.with_structured_output.return_value.invoke = invoke
    return model, invoke


def test_sparse_response_is_repaired_including_unsupported_claim_without_stale_errors():
    state = extracted()
    state['claims'].append(ResearchClaim(claim_id='unresolved', claim='A claim with no supporting passage.'))
    model, invoke = scripted(output(state, sparse=True), output(state))
    result = VerificationAgent(model)(state)
    assert invoke.call_count == 2
    assert result['step_status']['verification'] == 'completed'
    assert result['errors'] == []
    assert [claim.verdict for claim in result['verification_results']] == ['supported', 'supported', 'insufficient_evidence']
    for call in invoke.call_args_list:
        payload = json.loads(call.args[0][-1].content)
        assert len(payload['required_assessments']) == 3
        assert all(len(item['evidence_ids']) == 2 for item in payload['required_assessments'])
    assert 'format_repair' in json.loads(invoke.call_args_list[1].args[0][-1].content)


def test_repair_is_bounded_and_incomplete_judgments_still_fail_closed():
    state = extracted()
    sparse = output(state, sparse=True)
    model, invoke = scripted(sparse, sparse)
    result = VerificationAgent(model)(state)
    assert invoke.call_count == 2
    assert all(claim.verdict == 'insufficient_evidence' for claim in result['verification_results'])
    assert result['step_status']['verification'] == 'partial'
    assert any(error.kind == 'InvalidPassageAssessment' for error in result['errors'])


def test_repair_checks_other_passages_for_disagreement_instead_of_rubber_stamping():
    state = extracted()
    model, invoke = scripted(output(state, sparse=True), output(state, conflicting=True))
    result = VerificationAgent(model)(state)
    assert invoke.call_count == 2
    assert all(claim.disagreement and claim.verdict == 'insufficient_evidence'
               for claim in result['verification_results'])
    assert all(claim.supporting_source_ids and claim.contradicting_source_ids
               for claim in result['verification_results'])


def test_unknown_passage_reference_is_rejected_without_format_repair():
    state = extracted()
    invalid = output(state)
    for claim in invalid.results:
        claim.passage_assessments[0].evidence_id = 'invented-passage'
    model, invoke = scripted(invalid)
    result = VerificationAgent(model)(state)
    assert invoke.call_count == 1
    assert all(claim.verdict == 'insufficient_evidence' for claim in result['verification_results'])
    assert any(error.kind == 'InvalidVerification' for error in result['errors'])


def test_repair_only_requests_missing_claims_and_preserves_complete_results():
    state = extracted()
    complete = output(state)
    partial = VerificationOutput(results=complete.results[:1])
    replacement = VerificationOutput(results=complete.results[1:])
    model, invoke = scripted(partial, replacement)
    result = VerificationAgent(model)(state)
    assert result['step_status']['verification'] == 'completed'
    payload = json.loads(invoke.call_args_list[1].args[0][-1].content)
    assert [claim['claim_id'] for claim in payload['claims']] == [state['claims'][1].claim_id]
    assert all(claim.verdict == 'supported' for claim in result['verification_results'])


def test_format_repair_cannot_bypass_run_call_budget():
    state = extracted()
    model, invoke = scripted(output(state, sparse=True))
    result = ResearchRuntime(Settings(mock_mode=True, research_max_llm_calls=1)).wrap(
        'verification', VerificationAgent(model))(state)
    assert invoke.call_count == result['usage'].llm_calls == 1
    assert 'LLM-call limit' in result['usage'].limit_reason
    assert all(claim.verdict == 'insufficient_evidence' for claim in result['verification_results'])
    assert any('Verification format repair stopped' in error.message for error in result['errors'])
