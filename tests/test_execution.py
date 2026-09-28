import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, execution, store, worker


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(store,'DATA',Path(self.tmp.name)),
                      patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':'','VIDEO_AGENT_USER_CONCURRENT':'4'})]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_waiting_state_does_not_release_running_executor(self):
        j=store.create({});claimed=store.claim();token=claimed['execution']['token']
        with execution.scope(j['id'],token):
            store.update(j['id'],status='waiting_build',next_run_at=0)
        self.assertIsNone(store.claim())
        store.recover_queue()
        self.assertEqual(store.get(j['id'])['status'],'waiting_build')
        store.release(j['id'],token)
        self.assertEqual(store.claim()['id'],j['id'])

    def test_expired_owner_cannot_write_submit_renew_or_release_successor(self):
        j=store.create({});first=store.claim();old=first['execution']['token']
        with patch.object(store,'now',return_value=store.now()+store.LEASE_SECONDS+1):
            second=store.claim();new=second['execution']['token']
            self.assertNotEqual(old,new)
            with execution.scope(j['id'],old):
                for operation in (lambda:store.update(j['id'],status='failed'),
                                  lambda:store.event(j['id'],'stale'),
                                  lambda:store.reserve_submission(j['id'],{},4,20),
                                  lambda:store.renew(j['id'],old)):
                    with self.assertRaises(execution.ExecutionLost):operation()
            self.assertFalse(store.release(j['id'],old))
            with execution.scope(j['id'],new):store.update(j['id'],status='completed')

    def test_user_retry_cannot_modify_a_live_executor(self):
        j=store.create({});claimed=store.claim()
        with execution.scope(j['id'],claimed['execution']['token']):store.update(j['id'],status='failed')
        with self.assertRaises(execution.ExecutionBusy):store.update(j['id'],status='queued')

    def test_due_waiting_task_is_not_dispatched_twice_by_real_loop(self):
        j=store.create({});entered=threading.Event();release=threading.Event();stop=threading.Event();seen=[]
        def run(job):
            seen.append(job['id'])
            store.update(job['id'],status='waiting_build',next_run_at=0)
            entered.set();release.wait(4)
            store.update(job['id'],status='completed')
        with patch.object(worker,'STOP',stop),patch.object(worker,'process',side_effect=run),patch.object(config,'MAX_CONCURRENT_JOBS',2):
            thread=threading.Thread(target=worker.loop);thread.start()
            try:
                self.assertTrue(entered.wait(3))
                # More than one scheduler iteration while the task exposes waiting_build.
                self.assertFalse(stop.wait(1.2))
                self.assertEqual(seen,[j['id']])
            finally:
                release.set();stop.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(store.get(j['id'])['execution'])

    def test_subprocess_identity_is_explicit_and_not_shared_between_threads(self):
        with execution.scope('a','token-a'):
            self.assertEqual(execution.environment()['VIDEO_AGENT_EXECUTION_JOB'],'a')
            values=[]
            t=threading.Thread(target=lambda:values.append(execution.environment()));t.start();t.join()
            self.assertEqual(values,[{}])
        self.assertEqual(execution.environment(),{})

    def test_waiting_time_and_local_work_are_accounted_separately(self):
        with patch.object(store,'now',return_value=100):j=store.create({})
        with patch.object(store,'now',return_value=110):store.update(j['id'],status='planning')
        with patch.object(store,'now',return_value=125):store.update(j['id'],status='waiting_build')
        with patch.object(store,'now',return_value=225):store.update(j['id'],status='checking')
        self.assertEqual(store.get(j['id'])['status_seconds'],{'queued':10,'planning':15,'waiting_build':100})

    def test_claims_rotate_between_users(self):
        first=store.create({'owner':'alice'});store.create({'owner':'alice'});third=store.create({'owner':'bob'})
        claimed=store.claim();self.assertEqual(claimed['id'],first['id'])
        store.release(claimed['id'],claimed['execution']['token']);store.update(claimed['id'],status='completed')
        self.assertEqual(store.claim()['id'],third['id'])

    def test_two_five_ten_users_finish_without_duplicate_dispatch(self):
        for count in (2,5,10):
            with self.subTest(users=count):
                jobs=[store.create({'owner':f'batch{count}-user{i}'}) for i in range(count)]
                expected={j['id'] for j in jobs};seen=[];finished=threading.Event();stop=threading.Event();guard=threading.Lock()
                def run(job):
                    store.update(job['id'],status='completed')
                    with guard:
                        seen.append(job['id'])
                        if len(seen)==count:finished.set()
                with patch.object(worker,'STOP',stop),patch.object(worker,'process',side_effect=run),patch.object(config,'MAX_CONCURRENT_JOBS',2):
                    thread=threading.Thread(target=worker.loop);thread.start()
                    try:self.assertTrue(finished.wait(8))
                    finally:stop.set();thread.join(5)
                self.assertEqual(set(seen),expected);self.assertEqual(len(seen),len(expected))
                self.assertTrue(all(store.get(j['id'])['status']=='completed' for j in jobs))
