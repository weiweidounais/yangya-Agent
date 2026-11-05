"""Run the complete mock golden suite with opt-in hybrid routing and a mock scorer.

No model downloads or provider calls. This tests routing integration, not model
accuracy; exemplar calibration remains a separate experiment.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden import run_golden
from app.agent import intent_scoring, router


def mock_score(text):
    label, confidence = {
        "不知道下一步做什么": ("unresolved", .7),
        "我把东西都安排好了，以后不用操心了": ("crisis", .9),
    }.get(text, ("chat", .2))
    scores = {name: .1 for name in intent_scoring.LABELS}
    scores[label] = .8
    return scores, dict(confidence=confidence, margin=.1, winner=label,
                        model_version="golden-mock", exemplar_version="test", hosted=False)


def routing_probes():
    from app.data_access import user_scope

    # The router invokes audited registry tools, even with a mocked scorer.
    # These harness requests need their own trusted owner, just like golden cases.
    with user_scope("golden-hybrid-probes"):
        for text, intent, decision, source in [
            ("不知道下一步做什么", "chat", "clarify", "embedding_knn"),
            ("你好", "chat", "fallback", "embedding_knn"),
            # Embeddings may add a safety catch the lexicon missed.
            ("我把东西都安排好了，以后不用操心了", "chat", "route", "safety_override"),
        ]:
            result = router.classify_text_result(text)
            assert (result.intent, result.decision) == (intent, decision), (text, result)
            assert result.score_source == source, (text, result)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if run_golden._parse_args(args).mode != "mock":
        raise ValueError("hybrid mock scorer is only supported with --mode mock")
    with patch.dict(os.environ, {"INTENT_SCORING_MODE": "hybrid"}), \
         patch.object(intent_scoring, "warmup"), \
         patch.object(intent_scoring, "score_text", side_effect=mock_score) as scorer, \
         patch.object(intent_scoring, "second_opinion", return_value=False):
        def before_cases():
            routing_probes()
            scorer.reset_mock()
        status = run_golden.main(args, before_cases=before_cases)
        assert scorer.call_count > 0, "golden suite must exercise hybrid scoring"
        print(f"HYBRID mock scorer: 3 routing probes passed; {scorer.call_count} golden scoring calls")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
