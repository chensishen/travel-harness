from datetime import UTC, datetime, timedelta

from backend.app.store import Store
from backend.harness.progress import progress_snapshot

BASE = datetime(2026, 10, 29, 12, tzinfo=UTC)


def event(node, status, seconds, **extra):
    return {"node":node, "status":status,"at":(BASE+timedelta(seconds=seconds)).isoformat(),"message":"读取官网",**extra}


def test_substep_updates_do_not_reset_stage_clock_and_eta_overrun_is_explicit():
    job = {"status":"running","node":"Researcher","run_started_at":BASE.isoformat(),"planned_nodes":["Researcher","Verifier"]}
    events = [event('Researcher','running',0,phase_start=True),event('Researcher','running',90)]
    p = progress_snapshot(job,events,{'Researcher':[20,30,40],'Verifier':[50]},current=BASE+timedelta(seconds=100))
    stage = next(s for s in p['stages'] if s['id']=='Researcher')
    assert stage['elapsed_s']==100 and p['elapsed_s']==100
    assert p['estimate_delayed'] and p['remaining_low_s']>0
    assert p['estimated_finish_at'] and p['total_stages']==2


def test_retry_does_not_count_previous_run_future_stages_as_completed():
    job = {'status':'running','node':'Writer','run_started_at':(BASE+timedelta(seconds=60)).isoformat(),'planned_nodes':['Writer','Reviewer','Assembler']}
    events=[event('Writer','completed',20,duration_s=20),event('Reviewer','completed',30,duration_s=10),event('Assembler','completed',40,duration_s=10),event('Writer','running',60,phase_start=True)]
    p=progress_snapshot(job,events,{},current=BASE+timedelta(seconds=70))
    assert p['completed_stages']==0
    assert next(s for s in p['stages'] if s['id']=='Reviewer')['status']=='pending'
    assert next(s for s in p['stages'] if s['id']=='Writer')['elapsed_s']==10


def test_failed_job_stops_total_clock_and_keeps_measured_stage_duration():
    job={'status':'failed','node':'Writer','run_started_at':BASE.isoformat(),'run_finished_at':(BASE+timedelta(seconds=8)).isoformat(),'planned_nodes':['Writer']}
    p=progress_snapshot(job,[event('Writer','running',0),event('Writer','failed',8,duration_s=7.8)],{},current=BASE+timedelta(hours=3))
    assert p['elapsed_s']==8 and p['estimated_finish_at'] is None
    assert next(s for s in p['stages'] if s['id']=='Writer')['elapsed_s']==7.8


def test_kb_job_only_has_three_phases_and_queued_eta_is_not_promised():
    p=progress_snapshot({'kind':'kb','status':'queued','node':'Router','spec':None},[],{},current=BASE)
    assert p['total_stages']==3 and len(p['stages'])==3
    assert '历史样本不足' in p['estimate_basis'] and p['estimated_finish_at'] is None


def test_culture_intro_does_not_show_route_planner_stage():
    p=progress_snapshot({'kind':'document','status':'queued','node':'Router','spec':{'type':'culture_intro'}},[],{},current=BASE)
    ids=[s['id'] for s in p['stages']]
    assert 'RoutePlanner' not in ids and p['total_stages']==len(ids)==8


def test_culture_intro_planned_route_planner_is_not_counted():
    job={'status':'completed','node':'Assembler','spec':{'type':'culture_intro'},
         'planned_nodes':['Writer','RoutePlanner','Illustrator','Reviewer','Assembler']}
    p=progress_snapshot(job,[],{},current=BASE)
    assert 'RoutePlanner' not in [s['id'] for s in p['stages']]
    assert p['total_stages']==4


def test_samples_use_latest_success_per_job_and_exclude_reused_results(tmp_path):
    store=Store(tmp_path)
    store.event('a',node='Writer',status='completed',duration_s=10)
    store.event('a',node='Writer',status='completed',duration_s=20)
    store.event('b',node='Writer',status='completed',duration_s=0)
    store.event('c',node='Writer',status='failed',duration_s=300)
    assert store.timing_samples()=={'Writer':[20]}


def test_historical_jobs_show_recorded_duration_without_including_days_between_retries():
    p=progress_snapshot({'status':'completed','node':'Assembler'},[event('Writer','completed',10,duration_s=10),event('Assembler','completed',86400,duration_s=5)],{},current=BASE+timedelta(days=3))
    assert p['elapsed_s']==15 and p['elapsed_label']=='已记录阶段耗时'
async def test_cancel_during_revision_freezes_timing_and_records_interruption(tmp_path):
    import asyncio
    import json

    import pytest

    from backend.app.config import Settings
    from backend.harness.graph import Harness

    store = Store(tmp_path)
    store.save({'id':'revision-test','kind':'document','status':'queued','node':'Revision','revision_instruction':'修改颜色'})
    folder = tmp_path / 'jobs' / 'revision-test'
    folder.mkdir(parents=True)
    (folder / 'state.json').write_text(json.dumps({}))
    harness = Harness(Settings(_env_file=None), store)
    entered = asyncio.Event()

    async def wait_for_model(*args):
        entered.set()
        await asyncio.Event().wait()

    harness.models.json = wait_for_model
    task = asyncio.create_task(harness.run('revision-test', 'Revision'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = store.get('revision-test')
    assert job['status'] == 'interrupted' and job['run_finished_at']
    events = store.events('revision-test')
    assert events[-1]['status'] == 'interrupted' and events[-1]['duration_s'] >= 0
    progress = progress_snapshot(job, events, {})
    assert progress['estimated_finish_at'] is None
    assert progress['stages'][0]['status'] == 'interrupted'


def test_legacy_failed_stage_stops_at_last_recorded_event_without_finish_timestamp():
    job = {'status': 'failed', 'node': 'Verifier'}
    events = [event('Verifier', 'running', 0), event('Verifier', 'failed', 8)]
    progress = progress_snapshot(job, events, {}, current=BASE + timedelta(days=6))
    stage = next(s for s in progress['stages'] if s['id'] == 'Verifier')
    assert stage['elapsed_s'] == 8
    assert progress['elapsed_s'] == 8
