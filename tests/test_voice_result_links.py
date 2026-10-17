import pytest

from test_mcp_events import setup
from test_voice_analysis import voice_fixture
from test_voice_join import another, join_args
from test_question_result_greeting import result_context, results
from projects_hub.live_adapter import ProjectsHubLiveAdapter
from projects_hub.store import StoreError


@pytest.mark.asyncio
@pytest.mark.parametrize('publishing_branch',[None,'record/published'])
async def test_live_source_links_use_configured_branch_and_reach_mira(setup,publishing_branch):
    store,events,owner,service,analysis,repo,bridge,payload=voice_fixture(setup)
    original_connection=store.get_repository_connection
    try:
        if publishing_branch:
            bridge.config['source_branch']=publishing_branch
        accepted=await bridge.accept(payload)
        donor=another(payload);await bridge.accept(donor)
        joined=await bridge.joins.link(service['id'],**join_args(owner,payload,donor))
        read=events.get_project_request(service['id'],owner['project_id'],joined['request_id'])
        workspace=store.db.execute('SELECT workspace_id FROM projects WHERE id=?',(owner['project_id'],)).fetchone()[0]
        def source_connection(actor_id,workspace_id,repository_id):
            assert (actor_id,workspace_id,repository_id)==(owner['actor_id'],workspace,321)
            return dict(full_name='owner/intake',default_branch='notes/live',state='available',installation_state='active',role='project_docs',project_id=owner['project_id'])
        store.get_repository_connection=source_connection
        result=dict(kind='analysis_result',source_revision=1,summary='Результат готов',result_markdown='## Решение\nУточнение не требуется.',used_receipts=[])
        saved=await events.record_request_result_async(service['id'],owner['project_id'],read['request_id'],read['event_id'],result)
        links=saved['question_package']['delivery_links']
        expected_ref='record%2Fpublished' if publishing_branch else 'notes%2Flive'
        assert links['source_note_url']=='https://github.com/owner/intake/blob/'+expected_ref+'/intake/session.md'
        assert links['source_notes'][0]['branch_source']==('approved_source_binding' if publishing_branch else 'repository_default_fallback')
        assert len(links['source_notes'])==2
        assert links['source_notes'][0]['snapshot_url']=='https://github.com/owner/intake/blob/'+('c'*40)+'/intake/session.md'
        assert links['source_notes'][0]['projection_state']=='managed_by_intake'
        assert links['source_note_url']!=saved['result']['artifact_url']
        assert links['slack_result_url'] is None and links['slack_thread_url'] is None
        adapter=ProjectsHubLiveAdapter(store,collaboration=bridge.collaboration,collaboration_analysis=analysis,question_events=events)
        brief=adapter._personal_startup_brief(owner['actor_id'],workspace)
        ready=[i for i in brief['personal'] if i['kind']=='question_result_ready']
        assert len(ready)==1 and ready[0]['delivery_links']==links
        assert ready[0]['availability']=='available'
        assert len(adapter._personal_startup_brief(owner['actor_id'],workspace)['personal'])==len(brief['personal'])
        # Revoked source links disappear; existing readable PH result stays available.
        def revoked(*args):
            raise StoreError('GITHUB_REPOSITORY_UNAVAILABLE','Revoked source')
        store.get_repository_connection=revoked
        later=analysis.get_analysis(actor_id=owner['actor_id'],workspace_id=workspace,analysis_id=saved['analysis_id'])
        assert later['delivery_links']['source_note_url'] is None
        assert later['result_markdown']==result['result_markdown']
    finally:
        store.get_repository_connection=original_connection
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed',[True,False])
async def test_only_confirmed_slack_receipt_produces_result_permalink(setup,confirmed):
    store,events,analysis,ready,adapter,initialize=await result_context(setup,confirmed=confirmed)
    try:
        item=results(initialize())[0]
        links=item['delivery_links']
        assert links['source_note_url'] is None
        if confirmed:
            assert links['slack_result_url']=='https://app.slack.com/archives/C0123456789/p1791900099000002?thread_ts=1791900001.000001&cid=C0123456789'
            assert links['slack_thread_url']=='https://app.slack.com/archives/C0123456789/p1791900001000001'
        else:
            assert links['slack_result_url'] is None and links['slack_thread_url'] is None
    finally:
        await analysis.close()
