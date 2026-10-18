import json
import os
import stat
from pathlib import Path
import pytest
from scripts.provision_voice_analysis_binding import provision as raw_provision, ProvisionError, KEY


OWNER, PROJECT = 'usr_'+'a'*32, 'prj_'+'b'*32
BINDING = dict(owner_id=OWNER, project_id=PROJECT, client_id='fixture_client',
               source_repository_id=123, source_repository='fixture/private-notes')


def provision(*args, **kwargs):
    return raw_provision(*args, **BINDING, **kwargs)


@pytest.fixture
def paths(tmp_path):
    root=tmp_path/'private'
    root.mkdir(mode=0o700)
    env=root/'service.env'
    env.write_bytes(b'EXISTING="preserved"\nSECRET="test-only-value"\n')
    env.chmod(0o600)
    return root/'ph.json',root/'intake.json',env


def test_default_dry_run_creates_nothing(paths):
    before=paths[2].read_bytes()
    result=provision(*paths)
    assert result['state']=='dry_run_ready'
    assert not paths[0].exists() and not paths[1].exists()
    assert paths[2].read_bytes()==before


def test_apply_same_token_exact_scope_preserves_env_and_refuses_rotation(paths,capsys):
    before=paths[2].read_bytes()
    result=provision(*paths,apply=True)
    assert result['state']=='provisioned' and result['intake_mounted'] is False
    ph,intake=(json.loads(p.read_text()) for p in paths[:2])
    assert ph['token']==intake['token'] and len(ph['token'])>=48
    assert ph['owner_id']==OWNER and ph['project_id']==PROJECT
    assert ph['source_prefix']=='inbox/voice' and ph['source_branch']=='main'
    assert intake['enabled'] is True
    assert set(intake)=={'enabled','owner_id','endpoint','token'}
    assert paths[2].read_bytes()==before+KEY+b'="'+str(paths[0]).encode()+b'"\n'
    assert all(stat.S_IMODE(p.stat().st_mode)==0o600 for p in paths)
    assert capsys.readouterr().out==''
    original=paths[0].read_bytes()
    with pytest.raises(ProvisionError,match='existing_configuration_refused'):
        provision(*paths,apply=True)
    assert paths[0].read_bytes()==original


@pytest.mark.parametrize('index',[0,1])
def test_existing_partial_configuration_never_read_or_overwritten(paths,index):
    paths[index].write_text('unknown credential')
    before=paths[2].read_bytes()
    with pytest.raises(ProvisionError,match='existing_configuration_refused'):
        provision(*paths,apply=True)
    assert paths[index].read_text()=='unknown credential' and paths[2].read_bytes()==before


def test_symlinks_and_nonprivate_parents_refused(paths,tmp_path):
    target=tmp_path/'outside'
    target.write_text('untouched')
    paths[0].symlink_to(target)
    with pytest.raises(ProvisionError): provision(*paths,apply=True)
    assert target.read_text()=='untouched'
    paths[0].unlink()
    paths[0].parent.chmod(0o755)
    with pytest.raises(ProvisionError,match='private_parent_required'): provision(*paths)


def test_parent_symlink_refused(paths,tmp_path):
    link=tmp_path/'link'
    link.symlink_to(paths[0].parent,target_is_directory=True)
    with pytest.raises(OSError): provision(link/'ph.json',paths[1],paths[2])


@pytest.mark.parametrize('line',[KEY+b'="existing"\n',b'PROJECTS_HUB_VOICE_ANALYSIS_CONFIG={}\n'])
def test_existing_pointer_refused_without_new_files(paths,line):
    paths[2].write_bytes(line)
    with pytest.raises(ProvisionError,match='existing_binding_pointer_refused'): provision(*paths,apply=True)
    assert not paths[0].exists() and not paths[1].exists()


def test_concurrent_environment_change_retains_new_credentials_without_reuse(paths,monkeypatch):
    import scripts.provision_voice_analysis_binding as module
    original=module.write_new
    def changed(fd,name,content):
        original(fd,name,content)
        if name.startswith('.voice-binding-service-'):
            paths[2].write_bytes(b'OTHER_OPERATOR=changed\n')
    monkeypatch.setattr(module,'write_new',changed)
    with pytest.raises(ProvisionError,match='partial_or_failed_apply'):
        provision(*paths,apply=True)
    assert paths[2].read_bytes()==b'OTHER_OPERATOR=changed\n'
    assert paths[0].exists() and paths[1].exists()
    with pytest.raises(ProvisionError,match='existing_configuration_refused'): provision(*paths,apply=True)

@pytest.mark.parametrize('field,value',[
    ('owner_id','not-an-owner'),('project_id','prj_../other'),('client_id','bad\nclient'),
    ('source_repository_id',0),('source_repository','owner/../../other')])
def test_invalid_binding_arguments_refused_before_writes(paths,field,value):
    invalid={**BINDING,field:value}
    with pytest.raises(ProvisionError,match='invalid_binding_arguments'):
        raw_provision(*paths,**invalid,apply=True)
    assert not paths[0].exists() and not paths[1].exists()
