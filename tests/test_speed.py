"""Concurrency and bundled-workflow regressions; no paid model requests."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import config, store, agent_tools, production, unit_planner, worker, media
from app.production_schema import file_hash, context_version


def audio_fixture(path):
    import wave
    with wave.open(str(path), "wb") as sound:
        sound.setnchannels(1);sound.setsampwidth(2);sound.setframerate(16000)
        sound.writeframes(b"\x00\x08"*16000)


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name).resolve()
        self.ps = [patch.object(config, 'DATA', self.data), patch.object(store, 'DATA', self.data)]
        for p in self.ps:p.start()
        self.job = store.create({'prompt': '展示小鸡', 'images': [], 'reference': None,
            'ratio': '16:9', 'duration': 8, 'production_version': 2, 'engine': 'hypit-agent-v5'})
        self.jid = self.job['id']
        self.root = agent_tools.prepare(self.job)
        production.initialize(self.job, self.root)
        Image.new('RGB', (64, 64), 'yellow').save(self.root/'assets/chicken.jpg')
        subject = {'version': 'subject-v1', 'inputs': [{'path': 'assets/chicken.jpg',
            'sha256': file_hash(self.root/'assets/chicken.jpg')}],
            'assets': [{'path': 'assets/chicken.jpg', 'role': 'identity'}],
            'replacement_rules': ['完整替换主体']}
        store.update(self.jid, subject_spec=subject)
        self.plan = {'theme': '小鸡展示', 'segments': [{'duration': 4}, {'duration': 4}],
            'units': [{'id': 'a', 'description': '小鸡向右挥手', 'duration': 4,
                'continuity': {'type': 'independent', 'reason': '开场'}},
                {'id': 'b', 'description': '接着举起星星', 'duration': 4, 'depends_on': ['a'],
                'continuity': {'type': 'same_action', 'reason': '动作延续'}}]}
        unit_planner.validate(self.fresh(), self.root, self.plan)
        store.update(self.jid, plan=self.plan)

    def tearDown(self):
        for p in reversed(self.ps):p.stop()
        self.tmp.cleanup()

    def fresh(self):return store.get(self.jid)
    def call(self, action, **args):return agent_tools.dispatch(self.jid, action, args)
    def request(self, unit='a'):
        return {'unit_id': unit, 'prompt': '黄色毛绒小鸡在桌面上缓慢挥手，镜头推进并展示清楚的脸部与毛绒外观。'}

    def accept(self, name='a', dependencies=None):
        video = self.root/f'{name}.mp4';video.write_bytes(name.encode())
        frame = self.root/f'assets/end-{name}.jpg'
        Image.new('RGB', (64, 64), 'yellow').save(frame)
        r = {'version': f'review-{name}', 'verdict': 'pass', 'path': f'{name}.mp4',
            'sha256': file_hash(video), 'original_sha256': file_hash(video),
            'context_version': context_version(self.fresh()), 'dependencies': dependencies or {},
            'end_frame': str(frame.relative_to(self.root)), 'end_frame_sha256': file_hash(frame),
            'usable_range': [0, 4], 'observation': {'visual': 'full evidence stored on disk'}}
        store.update(self.jid, unit_reviews={**self.fresh().get('unit_reviews', {}), name: r})
        return r

    def test_generate_unit_is_one_agent_call_and_reuses_submission(self):
        with patch.object(agent_tools.hypit, 'check', return_value={'ok': True}), \
             patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build', return_value={'build': {'id': 'build-a'}}) as submit:
            first = self.call('generate_unit', **self.request())
            second = self.call('generate_unit', **self.request())
        self.assertEqual(first['build_id'], second['build_id'])
        submit.assert_called_once()
        self.assertEqual(self.fresh()['reserved_generation_seconds'], 4)
        self.assertEqual(self.fresh()['tool_calls'], 2)
        self.assertEqual(self.fresh()['tool_timings']['generate_unit']['calls'], 2)
        manifest = next(iter(self.fresh()['generation_manifests'].values()))
        self.assertEqual(manifest['args']['images'], ['assets/chicken.jpg'])
        self.assertEqual(manifest['output'], 'clip.video')

    def test_generate_unit_binds_verified_dependency_frame(self):
        review = self.accept()
        with patch.object(agent_tools.hypit, 'check'), patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build', return_value={'build': {'id': 'build-b'}}):
            self.call('generate_unit', **self.request('b'))
        manifest = next(iter(self.fresh()['generation_manifests'].values()))
        self.assertEqual(manifest['dependencies'], {'a': 'review-a'})
        self.assertIn(review['end_frame'], manifest['args']['images'])
        self.assertIn('assets/chicken.jpg', manifest['args']['images'])

    def test_unreviewed_dependency_cannot_use_shortcut(self):
        with patch.object(agent_tools.hypit, 'build') as submit:
            with self.assertRaisesRegex(ValueError, '尚未通过'):
                self.call('generate_unit', **self.request('b'))
        submit.assert_not_called()

    def test_changed_identity_cannot_use_shortcut(self):
        Image.new('RGB', (64, 64), 'red').save(self.root/'assets/chicken.jpg')
        with patch.object(agent_tools.hypit, 'build') as submit:
            with self.assertRaisesRegex(ValueError, '素材已变化'):
                self.call('generate_unit', **self.request())
        submit.assert_not_called()

    def test_uncertain_submission_is_not_repeated(self):
        with patch.object(agent_tools.hypit, 'check'), patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build', side_effect=RuntimeError('lost receipt')) as submit:
            with self.assertRaisesRegex(RuntimeError, 'lost receipt'):
                self.call('generate_unit', **self.request())
            with self.assertRaisesRegex(ValueError, '尚未确认'):
                self.call('generate_unit', **self.request())
        submit.assert_called_once()
        self.assertIsNotNone(self.fresh()['agent_submission_intent'])

    def test_budget_still_blocks_bundled_submission(self):
        store.update(self.jid, reserved_generation_seconds=30)
        with patch.object(agent_tools.hypit, 'check'), patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build') as submit:
            with self.assertRaisesRegex(ValueError, '预算不足'):
                self.call('generate_unit', **self.request())
        submit.assert_not_called()

    def test_reference_is_cut_to_planned_range_with_proof(self):
        ref = self.root/'assets/reference.mp4'
        media.command([config.FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=yellow:s=64x64:r=25:d=4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(ref)])
        analysis = {'source_sha256': file_hash(ref), 'status': 'verified', 'version': 'ref-v1',
            'candidate_boundaries': [], 'duration': 4, 'events': [{'id': 'e1', 'second': 0, 'description': '展示'}]}
        plan = copy.deepcopy(self.plan);plan['units'] = plan['units'][:1];plan['segments'] = [{'duration': 4}]
        plan['units'][0].update(event_ids=['e1'], reference_range=[0, 4])
        store.update(self.jid, reference='ref', reference_analysis=analysis, duration=4)
        unit_planner.validate(self.fresh(), self.root, plan);store.update(self.jid, plan=plan)
        with patch.object(agent_tools.hypit, 'check'), patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build', return_value={'build': {'id': 'build-ref'}}):
            self.call('generate_unit', **self.request())
        manifest = next(iter(self.fresh()['generation_manifests'].values()))
        clipped = self.root/manifest['args']['videos'][0]
        proof = json.loads(clipped.with_suffix('.cut.json').read_text())
        self.assertEqual([proof['start'], proof['end']], [0, 4])
        self.assertEqual(proof['source_sha256'], file_hash(ref))
        self.assertEqual(proof['output_sha256'], file_hash(clipped))

    def test_compose_shortcut_uses_accepted_order_and_keeps_render_in_hypit(self):
        self.accept();self.accept('b', {'a': 'review-a'})
        with patch.object(media, 'probe', return_value={'format': {'duration': '4'}, 'streams': []}), \
             patch.object(agent_tools.hypit, 'check'), patch.object(agent_tools.hypit, 'plan'), \
             patch.object(agent_tools.hypit, 'build', return_value={'build': {'id': 'render'}}):
            result = self.call('compose_build')
        self.assertEqual(result['generation_seconds'], 0)
        self.assertEqual([c['unit_id'] for c in self.fresh()['composition']['clips']], ['a', 'b'])
        self.assertEqual(result['output'], 'final.video')

    def test_unaccepted_clip_blocks_compose_shortcut(self):
        self.accept()
        with patch.object(agent_tools.hypit, 'build') as submit:
            with self.assertRaisesRegex(ValueError, '尚未通过'):
                self.call('compose_build')
        submit.assert_not_called()

    def test_collect_review_reuses_only_current_accepted_output(self):
        self.accept()
        build = {'build_id': 'build-a', 'unit_id': 'a', 'state': 'complete',
            'collected_path': 'a.mp4', 'collected_sha256': file_hash(self.root/'a.mp4')}
        store.update(self.jid, agent_builds={'a': build})
        with patch('app.unit_review.review') as review, patch.object(agent_tools.hypit, 'get') as collect:
            result = self.call('collect_review', build_id='build-a')
            self.assertEqual(result['verdict'], 'pass')
            self.assertNotIn('observation', result)
            self.assertIn('observation', self.fresh()['unit_reviews']['a'])
            review.assert_not_called();collect.assert_not_called()
            store.update(self.jid, prompt='新需求')
            with self.assertRaisesRegex(ValueError, '过期'):
                self.call('collect_review', build_id='build-a')

    def test_pending_or_foreign_build_cannot_be_reviewed(self):
        store.update(self.jid, agent_builds={'a': {'build_id': 'pending', 'unit_id': 'a', 'state': 'pending'}})
        with patch.object(agent_tools.hypit, 'get') as collect:
            for bid in ['pending', 'foreign']:
                with self.assertRaises(ValueError):self.call('collect_review', build_id=bid)
        collect.assert_not_called()

    def test_visual_and_audio_observers_overlap_and_cache_complete_evidence(self):
        clip = self.root/'test.mp4';clip.write_bytes(b'fixture');audio_fixture(clip.with_suffix('.wav'))
        (self.root/'evidence').mkdir()
        info = {'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}]}
        barrier = threading.Barrier(2, timeout=3)
        def chat(system, content, **kwargs):
            barrier.wait()  # A serialized implementation times out rather than silently passing.
            return {'speech':[], 'music':'music', 'effects':[], 'uncertainties':[]} if kwargs.get('model') else {'summary': 'yellow', 'events': []}
        with patch.object(agent_tools, 'range_clip', return_value=(clip, 0, 4, info)), \
             patch.object(media, 'command'), patch.object(agent_tools.planner, 'chat', side_effect=chat) as model:
            result = agent_tools.inspect_video(self.root, clip, {}, 'observe')
            self.assertTrue(result['audio']['checked'])
            self.assertEqual(result['visual']['summary'], 'yellow')
            self.assertEqual(agent_tools.inspect_video(self.root, clip, {}, 'observe'), result)
            self.assertEqual(model.call_count, 2)

    def test_audio_failure_is_explicit_and_does_not_poison_cache(self):
        clip = self.root/'test.mp4';clip.write_bytes(b'fixture');audio_fixture(clip.with_suffix('.wav'))
        (self.root/'evidence').mkdir()
        info = {'streams': [{'codec_type': 'audio'}]}
        def chat(system, content, **kwargs):
            if kwargs.get('model'):raise RuntimeError('temporary audio error')
            return {'events': []}
        with patch.object(agent_tools, 'range_clip', return_value=(clip, 0, 4, info)), patch.object(media, 'command'):
            with patch.object(agent_tools.planner, 'chat', side_effect=chat):
                failed = agent_tools.inspect_video(self.root, clip, {}, 'observe')
                self.assertFalse(failed['audio']['checked'])
            with patch.object(agent_tools.planner, 'chat', return_value={'speech':[], 'music':'music', 'effects':[], 'uncertainties':[]}) as model:
                recovered = agent_tools.inspect_video(self.root, clip, {}, 'observe')
                self.assertTrue(recovered['audio']['checked'])
                self.assertEqual(model.call_count, 2)

    def test_visual_json_is_not_accepted_as_audio_evidence(self):
        clip=self.root/'test.mp4';clip.write_bytes(b'fixture');audio_fixture(clip.with_suffix('.wav'))
        (self.root/'evidence').mkdir()
        with patch.object(agent_tools,'range_clip',return_value=(clip,0,4,{'streams':[{'codec_type':'audio'}]})), \
             patch.object(media,'command'),patch.object(agent_tools.planner,'chat',return_value={'shot_changes':[]}):
            result=agent_tools.inspect_video(self.root,clip,{},'observe')
        self.assertFalse(result['audio']['checked'])
        self.assertTrue(result['audio']['present'])

    def test_parallel_seam_failure_still_overrides_whole_film_pass(self):
        clip = self.root/'test.mp4';clip.write_bytes(b'fixture')
        barrier = threading.Barrier(2, timeout=3)
        def full(*args, **kwargs):
            barrier.wait()
            return {'verdict': 'pass', 'issues': [], 'summary': 'whole film',
                'checks': [{'status': 'pass', 'evidence': 'observed whole film'}]}
        def seams(*args, **kwargs):
            barrier.wait()
            return [{'at': 2, 'verdict': 'fail', 'issues': ['identity changed'], 'summary': 'bad seam'}]
        with patch.object(media, 'probe', return_value={'format': {'duration': 4}}), \
             patch.object(media, 'frames', return_value=[]), \
             patch.object(agent_tools, 'inspect_video', return_value={'audio': {'checked': False, 'reason': 'no audio track'}}), \
             patch.object(agent_tools.planner, 'chat', side_effect=full), \
             patch.object(agent_tools, 'review_joins', side_effect=seams):
            result = self.call('review', path='test.mp4')
        self.assertEqual(result['verdict'], 'fail')
        with self.assertRaises(ValueError):self.call('finish', path='test.mp4')

    def test_two_jobs_overlap_but_third_waits_and_each_is_claimed_once(self):
        jobs = [self.job, store.create({'prompt': 'two'}), store.create({'prompt': 'three'})]
        two_started = threading.Event();release = threading.Event();all_done = threading.Event()
        stop = threading.Event();lock = threading.Lock();seen = [];active = 0;peak = 0;done = 0
        def run(job):
            nonlocal active, peak, done
            with lock:
                seen.append(job['id']);active += 1;peak = max(peak, active)
                if active == 2:two_started.set()
            release.wait(5)
            store.update(job['id'], status='completed')
            with lock:
                active -= 1;done += 1
                if done == 3:all_done.set()
        with patch.object(worker, 'STOP', stop), patch.object(config, 'MAX_CONCURRENT_JOBS', 2), \
             patch.object(worker, 'process', side_effect=run):
            thread = threading.Thread(target=worker.loop);thread.start()
            try:
                self.assertTrue(two_started.wait(4))
                self.assertEqual(sum(j['status']=='queued' for j in store.list_jobs()), 1)
                release.set();self.assertTrue(all_done.wait(4))
            finally:
                stop.set();release.set();thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(peak, 2)
        self.assertCountEqual(seen, [j['id'] for j in jobs])

    def test_atomic_claim_never_assigns_one_job_twice(self):
        for n in range(4):store.create({'prompt': str(n)})
        with patch.dict('os.environ',{'VIDEO_AGENT_USER_CONCURRENT':'10'}), ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: store.claim(), range(8)))
        ids = [j['id'] for j in claims if j]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)


if __name__ == '__main__':unittest.main()
