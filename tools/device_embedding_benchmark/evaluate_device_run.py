#!/usr/bin/env python3
"""Score device-computed E5 queries against the existing PRIVATE benchmark matrix.
No model, API, database connection, upload or corpus mutation is performed.
Usage: python evaluate_device_run.py device.json --lab LAB --benchmark-root REPO
"""
from __future__ import annotations
import argparse, hashlib, json, math
from pathlib import Path
import numpy as np
MODEL = 'Xenova/multilingual-e5-small'
REVISION = '761b726dd34fb83930e26aab4e9ac3899aa1fa78'
BENCHMARK_COMMIT = 'f77f78427984fd970c85339c7de4f53a0f253004'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path, limit: int = 4_000_000):
    if path.stat().st_size > limit:
        raise ValueError(f'JSON too large: {path.name}')
    return json.loads(path.read_text(encoding='utf-8'))


def metrics(questions, rankings):
    positive = [q for q in questions if not q.get('unanswerable')]
    result = {'answerable_count': len(positive)}
    if not positive:
        return result
    for k in (1, 3, 5, 10):
        result[f'evidence_recall_at_{k}'] = sum(
            sum(bool(set(g) & set(rankings[q['id']][:k])) for g in q['evidence_groups']) / len(q['evidence_groups'])
            for q in positive) / len(positive)
        result[f'hit_at_{k}'] = sum(any(set(g) & set(rankings[q['id']][:k]) for g in q['evidence_groups']) for q in positive) / len(positive)
    result['mrr'] = sum(next((1 / (i + 1) for i, c in enumerate(rankings[q['id']]) if any(c in g for g in q['evidence_groups'])), 0) for q in positive) / len(positive)
    multi = [q for q in positive if len(q['evidence_groups']) > 1]
    result['multi_count'] = len(multi)
    result['all_evidence_at_10'] = (sum(all(set(g) & set(rankings[q['id']][:10]) for g in q['evidence_groups']) for q in multi) / len(multi)) if multi else None
    return result


def fusion(vector, lexical):
    scores = {}
    for ranking in (vector[:100], lexical[:100]):
        if len(ranking) != len(set(ranking)):
            raise ValueError('Duplicate ranked ID')
        for i, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0) + 1 / (60 + i)
    return sorted(scores, key=lambda x: (-scores[x], x))


def score(report_path: Path, lab: Path, repo: Path) -> dict:
    report = load_json(report_path)
    if report.get('schema') != 'rkb-device-embedding-run.v1':
        raise ValueError('Unknown device report schema')
    if report.get('status') not in {'completed_candidate', 'completed_review'}:
        raise ValueError('Interrupted/incomplete device run: not eligible for complete scoring')
    base = repo / 'scripts/benchmarks'
    qpath = base / 'small_embedding_questions.json'
    cpath = base / 'small_embedding_compatibility.json'
    fixture = load_json(qpath)
    reference = load_json(cpath)
    loaded = report.get('loaded', {})
    expected = {'model': MODEL, 'revision': REVISION, 'benchmark_commit': BENCHMARK_COMMIT,
                'questions_sha256': digest(qpath), 'fixture_sha256': digest(cpath), 'corpus_sha256': fixture['corpus_sha256']}
    for key, value in expected.items():
        if loaded.get(key) != value:
            raise ValueError(f'Reference mismatch: {key}; no mixing benchmark versions')
    corpus_path = lab / 'corpus.jsonl'
    if digest(corpus_path) != fixture['corpus_sha256']:
        raise ValueError('Private corpus snapshot changed')
    corpus = [json.loads(line) for line in corpus_path.read_text().splitlines()]
    ids = [row['id'] for row in corpus]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate corpus IDs')
    questions = fixture['questions']
    qids = [q['id'] for q in questions]
    rows = report.get('queries', [])
    if len(rows) != len(qids) or len({x['id'] for x in rows}) != len(rows) or {x['id'] for x in rows} != set(qids):
        raise ValueError('Device did not encode every question exactly once')
    by_id = {r['id']: r for r in rows}
    qvectors = np.asarray([by_id[qid]['vector'] for qid in qids], dtype=np.float32)
    docpath, refpath = lab / 'e5-document-vectors.npy', lab / 'e5-query-vectors.npy'
    docs = np.load(docpath, allow_pickle=False)
    refs = np.load(refpath, allow_pickle=False)
    if docs.shape != (len(ids), 384) or refs.shape != (len(qids), 384) or qvectors.shape != refs.shape:
        raise ValueError('Vector shape mismatch')
    for name, array in [('documents', docs), ('device queries', qvectors), ('reference queries', refs)]:
        if not np.isfinite(array).all() or not np.allclose(np.linalg.norm(array, axis=1), 1, atol=1e-4):
            raise ValueError(f'Invalid/nonunit vectors: {name}')
    lexical = load_json(lab / 'lexical.json')['rankings']
    if any(qid not in lexical for qid in qids):
        raise ValueError('Incomplete lexical baseline')
    def rank(matrix):
        scores = matrix @ docs.T
        return {q: [ids[j] for j in sorted(range(len(ids)), key=lambda j: (-float(scores[i, j]), ids[j]))] for i, q in enumerate(qids)}
    device, server = rank(qvectors), rank(refs)
    df = {q: fusion(device[q], lexical[q]) for q in qids}
    sf = {q: fusion(server[q], lexical[q]) for q in qids}
    slices = {'all': questions, 'natural': [q for q in questions if 'keyword_control' not in q['classes']], 'keywords': [q for q in questions if 'keyword_control' in q['classes']]}
    similarities = np.sum(qvectors * refs, axis=1) / (np.linalg.norm(qvectors, axis=1) * np.linalg.norm(refs, axis=1))
    negative = [q for q in questions if q.get('unanswerable')]
    quality = {mode: {name: metrics(qs, ranks) for name, qs in slices.items()} for mode, ranks in [('device_vector', device), ('device_fused', df), ('server_vector', server), ('server_fused', sf), ('lexical', lexical)]}
    drift = {q: {'top10_overlap_fraction': len(set(device[q][:10]) & set(server[q][:10])) / 10, 'device_first_known_evidence_rank': next((i + 1 for i, x in enumerate(device[q]) if any(x in g for g in questions[qids.index(q)]['evidence_groups'])), None)} for q in qids}
    return {'schema': 'rkb-device-retrieval-evaluation.v1', 'run_id': report['run_id'], 'device_report_sha256': digest(report_path), 'reference_commit': BENCHMARK_COMMIT, 'corpus_sha256': digest(corpus_path), 'document_matrix_sha256': digest(docpath), 'reference_query_matrix_sha256': digest(refpath), 'chunks': len(ids), 'questions': len(qids), 'quality': quality, 'query_cosine_min': float(similarities.min()), 'mean_top10_overlap': sum(x['top10_overlap_fraction'] for x in drift.values()) / len(drift), 'per_question': drift, 'negative_controls': {q['id']: {'device_returned_top10': len(device[q['id']][:10]), 'supports_answer': False, 'abstention_policy_tested': False} for q in negative}, 'compatibility_reported_by_device': report.get('compatibility', {}).get('pass'), 'limitations': ['Sparse original qrels: precision and answer accuracy are NOT established.', 'Corpus completeness has a known 1000-character-block truncation concern; this scores the historical snapshot only.', 'No new embeddings, network calls or production writes.', 'A successful browser run is not physical-phone or universal-device acceptance.']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=Path)
    p.add_argument('--lab', type=Path, required=True)
    p.add_argument('--benchmark-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = score(a.report, a.lab, a.benchmark_root)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'run_id': result['run_id'], 'quality': result['quality']['device_fused'], 'query_cosine_min': result['query_cosine_min'], 'mean_top10_overlap': result['mean_top10_overlap'], 'output': str(a.output)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
