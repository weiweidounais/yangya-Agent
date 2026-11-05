#!/usr/bin/env python3
"""No training: five-fold threshold selection plus held-out-band evaluation.

Draft labels are a scaffold, not production evidence. Uses SiliconFlow embeddings (SILICONFLOW_API_KEY required), without LLM calls.
This measures the automatic-execution band conservatively.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import intent_scoring as scoring
from app.agent.crisis_rules import has_crisis_signal
from app.agent.router import _classify_rules_result


def metrics(rows, high, low=.55):
    bands = {'auto': [], 'mid': [], 'low': []}
    for row in rows:
        band = 'auto' if row['eligible'] and row['confidence'] >= high else 'mid' if row['confidence'] >= low else 'low'
        bands[band].append(row)
    result = {}
    for band, values in bands.items():
        result[band] = {'count': len(values), 'coverage': len(values)/len(rows) if rows else 0,
                        'accuracy': sum(v['correct'] for v in values)/len(values) if values else None}
    return result


def select(rows, target):
    for high in [i/100 for i in range(56,101)]:
        auto = metrics(rows, high)['auto']
        if auto['count'] >= 10 and auto['accuracy'] >= target:
            return high
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default='intents/eval.jsonl')
    parser.add_argument('--output', default='intents/calibration-report.json')
    parser.add_argument('--target', type=float, default=.95)
    args = parser.parse_args()
    os.environ['INTENT_SCORING_MODE'] = 'hybrid'
    rows = [json.loads(line) for line in Path(args.dataset).read_text().splitlines()]
    if len(rows) < 200 or sum(row['confusable'] for row in rows) < len(rows)*.2:
        raise ValueError('need >=200 labeled examples and >=20% confusable')
    t0 = time.perf_counter()
    model, _, _, _ = scoring._resource()
    cold_ms = (time.perf_counter()-t0)*1000
    scoring.score_text('帮我设个提醒')  # warmup, excluded from percentiles
    timings, scored = [], []
    for row in rows:
        t0 = time.perf_counter()
        scores, evidence = scoring.score_text(row['text'])
        timings.append((time.perf_counter()-t0)*1000)
        pred = evidence['winner']
        crisis = has_crisis_signal(row['text'])
        legacy = _classify_rules_result(row['text'])
        scored.append({**row, 'prediction': 'crisis' if crisis else pred,
                       'confidence': evidence['confidence'], 'correct': ('crisis' if crisis else pred) == row['label'],
                       'eligible': not crisis and pred not in {'chat', 'unresolved', 'crisis'} and legacy.rule_id not in {'short_pass', 'ambiguous_pass'}})
    # Deterministic stratified folds keep each intent represented on both sides.
    rng = random.Random(34)
    grouped = {}
    for row in scored:
        grouped.setdefault(row['label'], []).append(row)
    folds = [[] for _ in range(5)]
    for group in grouped.values():
        rng.shuffle(group)
        for i, row in enumerate(group):
            folds[i % 5].append(row)
    reports, selected = [], []
    for i, heldout in enumerate(folds):
        train = [row for j, fold in enumerate(folds) if j != i for row in fold]
        high = select(train, args.target)
        if high is not None:
            selected.extend(row for row in heldout if row['eligible'] and row['confidence'] >= high)
        reports.append({'fold': i, 'selected_high': high, 'heldout': metrics(heldout, high if high is not None else 1.01)})
    import numpy as np
    # ECE is diagnostic only: these evidence scores are not calibrated probabilities.
    ece = 0.
    for lo in range(10):
        bucket = [r for r in scored if lo/10 <= r['confidence'] < (lo+1)/10 or (lo == 9 and r['confidence'] == 1)]
        if bucket:
            ece += len(bucket)/len(scored)*abs(sum(r['confidence'] for r in bucket)/len(bucket)-sum(r['correct'] for r in bucket)/len(bucket))
    low, high = scoring.thresholds()
    report = {'status': 'DRAFT labels — product review required; no model training',
              'model': model, 'exemplars_version': scoring.exemplars()['version'],
              'dataset_sha256': hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),
              'exemplars_sha256': hashlib.sha256(scoring.EXEMPLARS.read_bytes()).hexdigest(),
              'examples': len(rows), 'confusable': sum(r['confusable'] for r in rows),
              'cpu': platform.processor() or platform.machine(), 'embedding_provider': 'SiliconFlow',
              'cold_load_ms': cold_ms, 'latency_ms': {'p50': float(np.percentile(timings,50)), 'p95': float(np.percentile(timings,95)), 'samples': len(timings)},
              'default_thresholds': {'low': low, 'high': high}, 'default_bands': metrics(scored, high, low),
              'top1_accuracy': sum(r['correct'] for r in scored)/len(scored), 'evidence_score_ece': ece,
              'suggested_high_full_dataset_not_heldout': select(scored, args.target),
              'folds': reports, 'heldout_auto_count': len(selected),
              'heldout_auto_accuracy': sum(r['correct'] for r in selected)/len(selected) if selected else None,
              'score_formula': 'clip(min(top_k_mean + margin, margin / 0.1), 0, 1)',
              'target': args.target, 'target_met': bool(selected) and sum(r['correct'] for r in selected)/len(selected) >= args.target}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
