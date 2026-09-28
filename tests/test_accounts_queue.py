import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from app import config, store, auth, recovery
from app.server import app


class AccountQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root),
                      patch.dict(os.environ,{'VIDEO_AGENT_ENV':'local','VIDEO_AGENT_DATABASE_URL':'',
                        'VIDEO_AGENT_EMBEDDED_WORKER':'0','VIDEO_AGENT_USERS_JSON':'',
                        'VIDEO_AGENT_SESSION_SECRET':'a'*40,'VIDEO_AGENT_USER_CONCURRENT':'1'})]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_roles_ownership_revocation_and_plaintext_not_stored(self):
        auth.set_user('alice','alice-token-'+'a'*24)
        auth.set_user('bob','bob-token-'+'b'*24,role='admin')
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/jobs').status_code,401)
            r=client.post('/api/login',json={'username':'alice','token':'alice-token-'+'a'*24})
            self.assertEqual(r.status_code,200)
            cookie=client.cookies.get('hypit_session')
            j=client.post('/api/jobs',json={'prompt':'test','duration':8},headers={'Idempotency-Key':'one'}).json()
            again=client.post('/api/jobs',json={'prompt':'test','duration':8},headers={'Idempotency-Key':'one'}).json()
            self.assertEqual(j['id'],again['id'])
            self.assertEqual(client.get('/api/admin/status').status_code,403)
            self.assertEqual(client.post('/api/logout').status_code,200)
            client.cookies.set('hypit_session',cookie)
            self.assertEqual(client.get('/api/jobs').status_code,401)
            client.cookies.clear()
            client.post('/api/login',json={'username':'bob','token':'bob-token-'+'b'*24})
            self.assertEqual(client.get('/api/jobs/'+j['id']).status_code,404)
            self.assertEqual(client.get('/api/admin/status').status_code,200)
            auth.set_user('bob',enabled=False)
            self.assertEqual(client.get('/api/jobs').status_code,401)
        self.assertNotIn(b'alice-token-',(self.root/'tasks.sqlite').read_bytes())

    def test_user_limit_does_not_block_other_user_and_future_work_waits(self):
        a=store.create({'owner':'alice'});b=store.create({'owner':'alice'});other=store.create({'owner':'bob'})
        claimed=store.claim();self.assertEqual(claimed['id'],a['id'])
        self.assertEqual(store.claim()['id'],other['id'])
        self.assertIsNone(store.claim())
        store.release(a['id'],claimed['execution']['token'])
        store.update(a['id'],status='completed');store.update(b['id'],next_run_at=store.now()+60)
        self.assertIsNone(store.claim())
        store.update(b['id'],next_run_at=0)
        self.assertEqual(store.claim()['id'],b['id'])

    def test_poll_defers_without_resubmitting_or_sleeping(self):
        j=store.create({'agent_builds':{'one':{'build_id':'remote','state':'pending','run':'a.svrun','output':'clip.video'}}})
        root=self.root/'profile.json';root.write_text('{}')
        stop=Mock();stop.is_set.return_value=False
        with patch.object(recovery.hypit,'setup',return_value=root), \
             patch.object(recovery.hypit,'status',return_value={'build':{'work':{'state':'running'}}}), \
             patch.object(recovery.hypit,'build') as submit:
            with self.assertRaises(recovery.WorkDeferred):recovery.wait_builds(j,stop,once=True)
            submit.assert_not_called();stop.wait.assert_not_called()
        self.assertGreater(store.get(j['id'])['next_run_at'],store.now())
        self.assertEqual(store.get(j['id'])['status'],'waiting_build')

    def test_capacity_is_reserved_atomically_and_unknown_receipts_only_block_same_job(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_REMOTE_CONCURRENT':'1'}):
            a=store.create({});b=store.create({})
            intent={'record':{'generation_seconds':4}}
            self.assertTrue(store.reserve_submission(a['id'],intent,4,8))
            self.assertFalse(store.reserve_submission(b['id'],intent,4,8))
            self.assertNotIn('reserved_generation_seconds',store.get(b['id']))
            store.update(a['id'],agent_submission_intent=None,submission_uncertain={'id':'unknown'})
            self.assertTrue(store.reserve_submission(b['id'],intent,4,8))
            with self.assertRaisesRegex(ValueError,'不重复付费'):
                store.reserve_submission(a['id'],intent,4,8)

    def test_login_attempts_are_limited(self):
        for _ in range(10):auth.login_attempt('test')
        with self.assertRaises(Exception) as error:auth.login_attempt('test')
        self.assertEqual(error.exception.status_code,429)

    def test_explicit_exception_is_bound_to_one_request_and_one_paid_submission(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_MAX_UNCERTAIN_SUBMISSIONS':'1'}):
            old=store.create({'owner':'local'})
            store.update(old['id'],submission_uncertain={'id':'unknown'})
            doc={'owner':'local','prompt':'小鸡10秒','duration':10,'max_unit_generations':1}
            store.authorize_generation_request('local','approved',doc,max_seconds=10,max_submissions=1,note='explicit test authorization')
            with self.assertRaisesRegex(ValueError,'授权不匹配'):
                store.create({**doc,'duration':15},request_key='approved',require_generation=True)
            approved=store.create(dict(doc),request_key='approved',require_generation=True)
            self.assertEqual(store.create(dict(doc),request_key='approved',require_generation=True)['id'],approved['id'])
            self.assertFalse(store.generation_availability()['available'])
            with self.assertRaisesRegex(ValueError,'暂停新增'):
                store.create(dict(doc),request_key='other',require_generation=True)
            intent={'record':{'generation_seconds':10}}
            with self.assertRaisesRegex(ValueError,'授权的单次'):
                store.reserve_submission(approved['id'],intent,11,30)
            self.assertTrue(store.reserve_submission(approved['id'],intent,10,30))
            store.update(approved['id'],agent_submission_intent=None)
            with self.assertRaisesRegex(ValueError,'授权的单次'):
                store.reserve_submission(approved['id'],intent,4,30)
            self.assertTrue(store.get(old['id'])['submission_uncertain'])

    def test_global_receipt_pause_rejects_new_jobs_but_keeps_idempotent_receipts(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_MAX_UNCERTAIN_SUBMISSIONS':'1'}),TestClient(app) as client:
            payload={'prompt':'可爱小鸡跳舞','duration':10}
            headers={'Idempotency-Key':'original'}
            original=client.post('/api/jobs',json=payload,headers=headers).json()
            store.update(original['id'],status='needs_attention',submission_uncertain={'build_id':'unknown'})
            health=client.get('/api/health').json()['generation']
            self.assertFalse(health['available'])
            before=len(store.list_jobs())
            rejected=client.post('/api/jobs',json=payload,headers={'Idempotency-Key':'new'})
            self.assertEqual(rejected.status_code,503)
            self.assertEqual(len(store.list_jobs()),before)
            replay=client.post('/api/jobs',json=payload,headers=headers)
            self.assertEqual(replay.status_code,200)
            self.assertEqual(replay.json()['id'],original['id'])
            self.assertEqual(store.get(original['id'])['submission_uncertain']['build_id'],'unknown')

    def test_archiving_paused_unknown_receipts_restores_new_submission_without_losing_evidence(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_MAX_UNCERTAIN_SUBMISSIONS':'2'}),TestClient(app) as client:
            payload={'prompt':'可爱小鸡跳舞','duration':10}
            first=client.post('/api/jobs',json=payload,headers={'Idempotency-Key':'first'}).json()
            second=client.post('/api/jobs',json=payload,headers={'Idempotency-Key':'second'}).json()
            for job in (first,second):
                store.update(job['id'],status='needs_attention',submission_uncertain={'build_id':'unknown'})
            self.assertFalse(client.get('/api/health').json()['generation']['available'])
            with self.assertRaisesRegex(ValueError,'任务编号不存在'):
                store.archive_jobs([first['id'],'missing'],'用户暂不处理')
            self.assertEqual(len(store.list_jobs()),2)
            self.assertEqual(store.archive_jobs([first['id'],second['id']],'用户暂不处理'),
                             [first['id'],second['id']])
            self.assertEqual(store.archive_jobs([first['id'],second['id']],'用户暂不处理'),[])
            self.assertEqual(store.list_jobs(),[])
            self.assertEqual(len(store.list_jobs(include_archived=True)),2)
            self.assertTrue(store.get(first['id'])['submission_uncertain'])
            self.assertTrue(client.get('/api/health').json()['generation']['available'])
            replay=client.post('/api/jobs',json=payload,headers={'Idempotency-Key':'first'})
            self.assertEqual(replay.json()['id'],first['id'])
            fresh=client.post('/api/jobs',json=payload,headers={'Idempotency-Key':'fresh'})
            self.assertEqual(fresh.status_code,200)
            self.assertNotIn(fresh.json()['id'],(first['id'],second['id']))

    def test_disabling_last_user_never_enables_anonymous_mode(self):
        auth.set_user('only','long-secret-token-'+'x'*20)
        auth.set_user('only',enabled=False)
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/jobs').status_code,401)
            self.assertEqual(client.post('/api/login',json={'username':'only','token':'wrong'}).status_code,401)

    def test_waiting_generation_counts_towards_user_limit(self):
        a=store.create({'owner':'alice'});store.update(a['id'],status='waiting_build',next_run_at=store.now()+60)
        store.create({'owner':'alice'})
        b=store.create({'owner':'bob'})
        self.assertEqual(store.claim()['id'],b['id'])

    def test_stale_retry_cannot_requeue_a_running_task(self):
        j=store.create({});store.update(j['id'],status='failed')
        store.update(j['id'],_expected_statuses=('failed',),status='queued')
        self.assertEqual(store.claim()['id'],j['id'])
        with self.assertRaisesRegex(ValueError,'状态已变化'):
            store.update(j['id'],_expected_statuses=('failed',),status='queued')
        self.assertEqual(store.get(j['id'])['status'],'planning')

    def test_poll_deadline_survives_worker_restarts(self):
        j=store.create({'agent_builds':{'one':{'build_id':'remote','state':'pending','poll_started_at':store.now()-2500}}})
        stop=Mock();stop.is_set.return_value=False
        with patch.object(recovery.hypit,'status') as poll:
            with self.assertRaisesRegex(recovery.RecoveryPaused,'40分钟'):
                recovery.wait_builds(j,stop,once=True)
            poll.assert_not_called()
        self.assertEqual(store.get(j['id'])['agent_builds']['one']['build_id'],'remote')

    def test_daily_generation_budget_includes_repairs_without_recharging_prior_day(self):
        with patch.dict(os.environ,{'VIDEO_AGENT_DAILY_GENERATION_SECONDS':'8','VIDEO_AGENT_REMOTE_CONCURRENT':'10'}):
            old=store.create({'owner':'alice'})
            store.update(old['id'],created=store.now()-86400,reserved_generation_seconds=100)
            intent={'record':{'generation_seconds':4}}
            self.assertTrue(store.reserve_submission(old['id'],intent,4,120))
            other=store.create({'owner':'alice'})
            self.assertTrue(store.reserve_submission(other['id'],intent,4,120))
            third=store.create({'owner':'alice'})
            with self.assertRaisesRegex(ValueError,'今日预留'):
                store.reserve_submission(third['id'],intent,4,120)
            self.assertEqual(store.get(old['id'])['reserved_generation_seconds'],104)
            self.assertNotIn('agent_submission_intent',store.get(third['id']))
