"""Arithmetic/contract tests only. Synthetic vectors are not device evidence."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pytest
from evaluate_device_run import metrics, fusion, score, MODEL, REVISION, BENCHMARK_COMMIT


def test_evidence_groups_and_negative_exclusion():
    qs=[{'id':'a','evidence_groups':[['x','alt'],['y']]},{'id':'b','evidence_groups':[['z']]},{'id':'n','unanswerable':True,'evidence_groups':[]}]
    m=metrics(qs,{'a':['noise','x','y'],'b':[],'n':['noise']})
    assert m['evidence_recall_at_1']==0
    assert m['evidence_recall_at_5']==.5
    assert m['hit_at_5']==.5
    assert m['mrr']==.25
    assert m['all_evidence_at_10']==1


def test_rrf_ties_and_duplicate_rejection():
    assert fusion(['b','a'],['a','b'])==['a','b']
    with pytest.raises(ValueError): fusion(['a','a'],[])


@pytest.fixture
def synthetic_run(tmp_path):
    lab=tmp_path/'lab'; lab.mkdir()
    repo=tmp_path/'repo'; b=repo/'scripts'/'benchmarks'; b.mkdir(parents=True)
    corpus=''.join(json.dumps({'id':x,'text':'Synthetic '+x})+'\n' for x in ['a','b','c'])
    (lab/'corpus.jsonl').write_text(corpus)
    csha=hashlib.sha256(corpus.encode()).hexdigest()
    questions=[{'id':'q1','classes':['fact'],'evidence_groups':[['a'],['b']]},{'id':'q2','classes':['unanswerable'],'unanswerable':True,'evidence_groups':[]}]
    (b/'small_embedding_questions.json').write_text(json.dumps({'corpus_sha256':csha,'questions':questions}))
    (b/'small_embedding_compatibility.json').write_text('{}')
    docs=np.zeros((3,384),dtype='float32');docs[0,0]=1;docs[1,1]=1;docs[2,2]=1
    queries=np.zeros((2,384),dtype='float32');queries[0,0]=1;queries[1,2]=1
    np.save(lab/'e5-document-vectors.npy',docs);np.save(lab/'e5-query-vectors.npy',queries)
    (lab/'lexical.json').write_text(json.dumps({'rankings':{'q1':['b'],'q2':[]}}))
    loaded={'model':MODEL,'revision':REVISION,'benchmark_commit':BENCHMARK_COMMIT,'corpus_sha256':csha,
      'questions_sha256':hashlib.sha256((b/'small_embedding_questions.json').read_bytes()).hexdigest(),
      'fixture_sha256':hashlib.sha256((b/'small_embedding_compatibility.json').read_bytes()).hexdigest()}
    report={'schema':'rkb-device-embedding-run.v1','status':'completed_review','run_id':'synthetic-test','loaded':loaded,'queries':[{'id':q['id'],'vector':v.tolist()} for q,v in zip(questions,queries)]}
    path=tmp_path/'run.json';path.write_text(json.dumps(report))
    return path,lab,repo,report


def test_scoring_has_same_reference_ranking(synthetic_run):
    path,lab,repo,_=synthetic_run
    out=score(path,lab,repo)
    assert out['query_cosine_min']==1
    assert out['quality']['device_vector']==out['quality']['server_vector']
    assert out['quality']['device_fused']['natural']['all_evidence_at_10']==1
    assert out['negative_controls']['q2']['abstention_policy_tested'] is False


@pytest.mark.parametrize('kind',['missing','duplicate','nan','dimension','interrupted','bad_ref'])
def test_invalid_report_fails(synthetic_run,kind):
    path,lab,repo,r=synthetic_run
    if kind=='missing':r['queries'].pop()
    if kind=='duplicate':r['queries'][1]['id']='q1'
    if kind=='nan':r['queries'][0]['vector'][0]=float('nan')
    if kind=='dimension':r['queries'][0]['vector'].pop()
    if kind=='interrupted':r['status']='interrupted_hidden'
    if kind=='bad_ref':r['loaded']['revision']='different'
    path.write_text(json.dumps(r))
    with pytest.raises(ValueError):score(path,lab,repo)


def test_changed_corpus_fails(synthetic_run):
    path,lab,repo,_=synthetic_run
    with (lab/'corpus.jsonl').open('a') as f:f.write('\n')
    with pytest.raises(ValueError):score(path,lab,repo)
