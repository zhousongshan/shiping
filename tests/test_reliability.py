import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import config, store, runtime, agent_runner, recovery
from app.errors import classify, WorkflowError


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root),
                      patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':''})]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_uncertain_write_never_uses_network_retry_policy(self):
        for detail in ('fetch failed','Connection error','Request timed out','HTTP 503'):
            f=classify(detail,stage='submit',submission=True)
            self.assertEqual(f.code,'SUBMISSION_UNCERTAIN')
            self.assertFalse(f.retryable)
            self.assertTrue(f.submission_uncertain)
            self.assertTrue(classify(detail,stage='poll').retryable)

    def test_provider_preflight_is_not_an_unknown_paid_submission(self):
        detail='VIDEO_TRANSPORT_PREFLIGHT_FAILED: ECONNRESET connection failed'
        result=classify(detail,stage='generation',submission=True)
        self.assertEqual(result.code,'TRANSPORT_PREFLIGHT')
        self.assertFalse(result.submission_uncertain)
        self.assertFalse(result.retryable)
        # Never let the preflight marker clear separate evidence of a POST.
        result=classify(detail+' SUBMISSION_UNCERTAIN: lost receipt',stage='generation',submission=True)
        self.assertTrue(result.submission_uncertain)

    def test_auth_and_input_failures_not_retried_and_private_detail_hidden(self):
        for code in (400,401,403,422):
            f=classify('Bearer SECRET https://host/?signature=PRIVATE',stage='submit',http_status=code,submission=True)
            self.assertFalse(f.retryable)
            self.assertFalse(f.submission_uncertain)
            self.assertNotIn('SECRET',json.dumps(f.public()))
            self.assertNotIn('PRIVATE',json.dumps(f.public()))
        rejected=classify('HTTP 400 timeout while fetching resource',stage='submit',submission=True)
        self.assertFalse(rejected.submission_uncertain)

    def test_concurrent_submission_key_creates_only_one_job(self):
        def create(_):return store.create({'prompt':'跳舞','owner':'alice','duration':10},request_key='same')['id']
        with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(create,range(8)))
        self.assertEqual(len(set(ids)),1)
        with self.assertRaisesRegex(ValueError,'不同内容'):
            store.create({'prompt':'唱歌','owner':'alice','duration':10},request_key='same')
        other=store.create({'prompt':'跳舞','owner':'bob','duration':10},request_key='same')
        self.assertNotEqual(other['id'],ids[0])

    def test_events_are_not_lost_in_concurrent_writes(self):
        job=store.create({'owner':'alice'})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i:store.event(job['id'],str(i)),range(30)))
        self.assertEqual(len(store.get(job['id'])['events']),30)

    def test_browser_retry_across_code_upgrade_keeps_original_job(self):
        original=store.create({'prompt':'dance','owner':'alice','source_version':'old','workflow_version':3},request_key='same')
        restored=store.create({'prompt':'dance','owner':'alice','source_version':'new','workflow_version':3},request_key='same')
        self.assertEqual(original['id'],restored['id'])
        self.assertEqual(restored['source_version'],'old')

    def test_owner_is_filtered_before_history_limit(self):
        own=store.create({'owner':'alice'})
        for _ in range(101):store.create({'owner':'bob'})
        self.assertEqual([j['id'] for j in store.list_jobs(owner='alice')],[own['id']])

    def test_second_queue_leader_cannot_recover_live_jobs(self):
        with runtime.QueueLeader():
            with self.assertRaises(BlockingIOError):
                with runtime.QueueLeader():pass

    def test_production_missing_config_fails_closed(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_ENV':'production','VIDEO_AGENT_USERS_JSON':'',
                                    'VIDEO_AGENT_PUBLIC_URL':'','VIDEO_AGENT_MEDIA_BASE_URL':''}):
            with self.assertRaisesRegex(RuntimeError,'生产配置未就绪'):runtime.validate()

    def test_fast_process_exit_does_not_drop_final_model_error(self):
        job=store.create({'prompt':'test'})
        root=self.root/'project';root.mkdir()
        events=[{'type':'status','phase':'step_start','step':1} for _ in range(200)]
        events.append({'type':'status','phase':'turn_end','reason':{'kind':'error','error':{'code':'TRANSPORT','message':'Connection error.'}}})
        script='import sys;sys.stdin.read();sys.stdout.write('+repr('\n'.join(json.dumps(e) for e in events))+');sys.exit(1)'
        real_popen=subprocess.Popen
        def child(*args,**kwargs):return real_popen([sys.executable,'-c',script],**kwargs)
        with patch.object(agent_runner.agent_tools,'prepare',return_value=root), \
             patch.object(agent_runner,'profile',return_value=(os.environ.copy(),root/'profile')), \
             patch.object(agent_runner.subprocess,'Popen',side_effect=child):
            with self.assertRaises(WorkflowError) as failure:
                agent_runner.run_turn(job,'test',threading.Event())
        self.assertEqual(failure.exception.failure.code,'NETWORK')
        self.assertEqual(len((self.root/'agent-events.jsonl').read_text().splitlines()),201)

    def test_poll_disconnect_reattaches_to_receipted_task_without_new_provider_request(self):
        project=self.root/'project';journal=project/'submission-journal';journal.mkdir(parents=True)
        (journal/'accepted.json').write_text(json.dumps({'state':'submitted','id':'provider-123'}))
        build={'build_id':'local-old','state':'failed','run':'unit.svrun','output':'clip.video',
               'stamp_version':2,'generation_seconds':10}
        job=store.create({'agent_builds':{'stamp':build}})
        current={'failure':'poll failed: fetch failed [ECONNRESET]',
                 'operations':[{'receipt':{'id':'provider-123'}}]}
        with patch.object(recovery.agent_tools,'build_stamp',return_value='stamp'), \
             patch.object(recovery.hypit,'build',return_value={'build':{'id':'local-new'}}) as submit:
            self.assertTrue(recovery.resume_accepted_poll(job,project,'stamp',build,current,project/'runtime.json'))
        submit.assert_called_once()
        saved=store.get(job['id'])
        self.assertEqual(saved['agent_builds']['stamp']['build_id'],'local-new')
        self.assertEqual(saved['agent_builds']['stamp']['state'],'pending')
        self.assertIsNone(saved.get('agent_submission_intent'))
        self.assertEqual(saved['agent_builds']['stamp']['recovery_attempts'],1)

    def test_poll_disconnect_without_receipt_cannot_rebuild(self):
        project=self.root/'project';project.mkdir()
        build={'build_id':'local-old','state':'failed','run':'unit.svrun','output':'clip.video'}
        job=store.create({'agent_builds':{'stamp':build}})
        current={'failure':'poll failed: fetch failed [ECONNRESET]','operations':[]}
        with patch.object(recovery.hypit,'build') as submit:
            self.assertFalse(recovery.resume_accepted_poll(job,project,'stamp',build,current,project/'runtime.json'))
        submit.assert_not_called()

    def test_wan_receipt_is_recovered_by_get_without_creating_another_build(self):
        root=self.root/'project';journal=root/'submission-journal';journal.mkdir(parents=True)
        (journal/'receipt.json').write_text(json.dumps({'state':'submitted','id':'wan-one'}))
        profile=root/'runtime.json';profile.write_text(json.dumps({'bindings':{'@hypit/seedance@1#seedance-2':'wan'},'endpoints':{'wan':{'use':'@local/provider-autodl-wan'}}}))
        b={'build_id':'local-one','run':'a.svrun','output':'clip.video','state':'pending'}
        j=store.create({'agent_builds':{'stamp':b}})
        view={'failure':'poll failed: fetch failed','operations':[{'receipt':{'id':'wan-one'}}]}
        with patch.object(recovery.agent_tools,'build_stamp',return_value='stamp'),patch.object(recovery.hypit,'build') as submit:
            self.assertTrue(recovery.resume_accepted_poll(j,root,'stamp',b,view,profile))
            submit.assert_not_called()
        saved=store.get(j['id'])['agent_builds']['stamp']
        self.assertEqual(saved['build_id'],'local-one')
        self.assertEqual(saved['provider_task_id'],'wan-one')
        self.assertTrue(saved['provider_recovery'])

    def test_collect_transport_failure_preserves_paid_receipt_and_uses_get_recovery(self):
        root=self.root/'project';journal=root/'submission-journal';journal.mkdir(parents=True)
        (journal/'receipt.json').write_text(json.dumps({'state':'submitted','id':'paid-one'}))
        profile=root/'runtime.json';profile.write_text(json.dumps({'bindings':{'@hypit/seedance@1#seedance-2':'wan'},'endpoints':{'wan':{'use':'@local/provider-autodl-wan'}}}))
        build={'build_id':'local-one','run':'a.svrun','output':'clip.video','state':'failed'}
        job=store.create({'agent_builds':{'stamp':build}})
        view={'failure':'collect failed: fetch failed; ECONNRESET','operations':[{'receipt':{'id':'paid-one'}}]}
        with patch.object(recovery.agent_tools,'build_stamp',return_value='stamp'),patch.object(recovery.hypit,'build') as submit:
            self.assertTrue(recovery.resume_accepted_poll(job,root,'stamp',build,view,profile))
            submit.assert_not_called()
        self.assertEqual(store.get(job['id'])['agent_builds']['stamp']['provider_task_id'],'paid-one')


@unittest.skipUnless(os.getenv('HYPIT_TEST_DATABASE_URL'),'isolated PostgreSQL DSN not provided')
class PostgresTests(unittest.TestCase):
    def test_transactions_and_submission_keys_on_real_postgres(self):
        import uuid
        owner='test-'+uuid.uuid4().hex
        with patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':os.environ['HYPIT_TEST_DATABASE_URL']}):
            def create(_):return store.create({'prompt':'test','owner':owner,'duration':10},daily_limit=10,request_key='one')
            with ThreadPoolExecutor(max_workers=4) as pool:jobs=list(pool.map(create,range(8)))
            self.assertEqual(len({j['id'] for j in jobs}),1)
            job=jobs[0]
            store.update(job['id'],status='failed',error='test')
            store.event(job['id'],'test')
            self.assertEqual(store.get(job['id'])['status'],'failed')
            self.assertEqual(len(store.list_jobs(owner=owner)),1)
            with self.assertRaises(ValueError):
                store.create({'prompt':'two','owner':owner,'duration':10},daily_limit=10,request_key='two')
