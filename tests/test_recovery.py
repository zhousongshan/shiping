"""Recovery regressions with isolated storage and no paid provider requests."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app import config, store, recovery, hypit_adapter

MESSAGE = 'submit failed: Ark HTTP 400 ' + json.dumps({
    'code': 'InvalidParameter',
    'message': 'The parameter content[2].video_url is not valid: timeout while fetching resource.'})


def failed(message=MESSAGE, **extra):
    return {'build': {'id': 'old', 'work': {'state': 'done', 'outcome': 'failed'},
        'failure': message, 'operations': [{'endpoint': 'ark', 'state': 'failed',
            'failure': {'message': message}, **extra}]}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [patch.object(config, 'DATA', self.root), patch.object(store, 'DATA', self.root),
            patch.object(recovery.hypit, 'setup', side_effect=self.runtime), patch.object(recovery.agent_tools, 'build_stamp', return_value='stamp')]
        for p in self.patches:
            p.start()
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False

    def runtime(self,root):
        root.mkdir(parents=True,exist_ok=True);p=root/'hypit.runtime.json'
        p.write_text('{}');return p

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def job(self, **extra):
        return store.create({'agent_builds': {
            'done': {'build_id': 'completed', 'state': 'complete', 'run': 'one.svrun', 'output': 'clip.video'},
            'stamp': {'build_id': 'old', 'state': 'pending', 'run': 'three.svrun', 'output': 'clip.video'}},
            'reserved_generation_seconds': 30, **extra})

    def test_only_failed_unit_is_retried_then_resumed(self):
        j = self.job()
        views = [failed(), {'build': {'work': {'state': 'done', 'outcome': 'complete'}}}]
        with patch.object(recovery.hypit, 'status', side_effect=views) as status, patch.object(recovery.hypit, 'build', return_value={'build': {'id': 'retry'}}) as build:
            self.assertTrue(recovery.wait_builds(j, self.stop))
        self.assertEqual([c.args[1] for c in status.call_args_list], ['old', 'retry'])
        self.assertEqual(build.call_args.args[1], 'three.svrun')
        saved = store.get(j['id'])
        self.assertEqual(saved['agent_builds']['done'], j['agent_builds']['done'])
        self.assertEqual(saved['agent_builds']['stamp']['state'], 'complete')
        self.assertEqual(saved['agent_builds']['stamp']['recovery_history'], ['old'])
        self.assertEqual(saved['reserved_generation_seconds'], 30)
        self.assertIsNone(saved['agent_submission_intent'])

    def test_retries_are_bounded_and_survive_restart(self):
        j = self.job()
        with patch.object(recovery.hypit, 'status', side_effect=lambda *a: failed()), patch.object(recovery.hypit, 'build', side_effect=[{'build': {'id': 'r1'}}, {'build': {'id': 'r2'}}]) as build:
            with self.assertRaisesRegex(recovery.RecoveryPaused, '两次'):
                recovery.wait_builds(j, self.stop)
            self.assertEqual(build.call_count, 2)
            with self.assertRaisesRegex(recovery.RecoveryPaused, '两次'):
                recovery.wait_builds(store.get(j['id']), self.stop)
            self.assertEqual(build.call_count, 2)

    def test_manual_continue_starts_new_bounded_retry_cycle(self):
        j = self.job(retry_requested=True)
        j['agent_builds']['stamp'].update(state='failed', recovery_attempts=2)
        with patch.object(recovery.hypit, 'status', side_effect=[failed(), {'build': {'work': {'outcome': 'complete'}}}]), patch.object(recovery.hypit, 'build', return_value={'build': {'id': 'manual'}}) as build:
            recovery.wait_builds(j, self.stop)
        build.assert_called_once()
        self.assertFalse(store.get(j['id'])['retry_requested'])

    def test_other_failures_and_receipts_never_resubmit(self):
        for view in [failed('connection timeout'), failed(MESSAGE.replace('400', '500')),
                     failed(MESSAGE.replace('timeout while fetching resource', 'unsupported format')),
                     failed(receipt={'id': 'cgt-existing'})]:
            j = self.job()
            with patch.object(recovery.hypit, 'status', return_value=view), patch.object(recovery.hypit, 'build') as build:
                with self.assertRaises(recovery.RecoveryPaused):
                    recovery.wait_builds(j, self.stop)
                build.assert_not_called()

    def test_receipted_reference_download_failure_reports_cause_without_resubmission(self):
        j = self.job()
        message = 'Failed to download https://example.invalid/media/reference.mp4'
        view = failed(message, receipt={'id': 'provider-task'})
        view['build']['operations'][0]['endpoint'] = 'autodl-wan'
        view['build']['operations'][0]['failure']['code'] = 'InvalidParameter'
        with patch.object(recovery.hypit, 'status', return_value=view), patch.object(recovery.hypit, 'build') as build:
            with self.assertRaisesRegex(recovery.RecoveryPaused, '无法下载参考素材'):
                recovery.wait_builds(j, self.stop)
            build.assert_not_called()
        self.assertEqual(store.get(j['id'])['failure']['code'], 'REFERENCE_FETCH_FAILED')

    def test_changed_sources_never_resubmit(self):
        j = self.job()
        with patch.object(recovery.agent_tools, 'build_stamp', return_value='changed'), patch.object(recovery.hypit, 'status', return_value=failed()), patch.object(recovery.hypit, 'build') as build:
            with self.assertRaisesRegex(recovery.RecoveryPaused, '变化'):
                recovery.wait_builds(j, self.stop)
            build.assert_not_called()

    def test_uncertain_recovery_preserves_intent(self):
        j = self.job()
        with patch.object(recovery.hypit, 'status', return_value=failed()), patch.object(recovery.hypit, 'build', side_effect=RuntimeError('connection closed')) as build:
            with self.assertRaises(RuntimeError):
                recovery.wait_builds(j, self.stop)
        self.assertEqual(store.get(j['id'])['agent_submission_intent']['previous_build_id'], 'old')
        build.assert_called_once()
        with patch.object(recovery.hypit, 'build') as build:
            with self.assertRaisesRegex(recovery.RecoveryPaused, '尚未确认'):
                recovery.wait_builds(store.get(j['id']), self.stop)
            build.assert_not_called()

    def test_stop_during_backoff_preserves_original_id(self):
        j = self.job()
        self.stop.wait.return_value = True
        with patch.object(recovery.hypit, 'status', return_value=failed()), patch.object(recovery.hypit, 'build') as build:
            with self.assertRaisesRegex(RuntimeError, '停止'):
                recovery.wait_builds(j, self.stop)
            build.assert_not_called()
        self.assertEqual(store.get(j['id'])['agent_builds']['stamp']['build_id'], 'old')

    def test_failed_status_exit_code_still_returns_structured_result(self):
        view = {'format': 'hypit.cli-status@1', **failed()}
        run = subprocess.CompletedProcess([], 1, json.dumps(view), '')
        with patch.object(hypit_adapter.config, 'environment', return_value={}), patch.object(hypit_adapter.subprocess, 'run', return_value=run):
            self.assertEqual(hypit_adapter.status(self.root, 'old', self.root/'profile.json'), view)
            with self.assertRaises(RuntimeError):
                hypit_adapter.status(self.root, 'wrong-id', self.root/'profile.json')
            with self.assertRaises(RuntimeError):
                hypit_adapter.build(self.root, 'run.svrun', self.root/'profile.json')


if __name__ == '__main__':
    unittest.main()
