"""Offline invariants: no provider calls and no writes to the application database."""
import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from pydantic import ValidationError
from app import config, store, intent, agent_tools, compiler, media_gateway
from app import hypit_adapter
from app.server import NewJob

class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()
        self.patches=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def job(self,**extra):
        return store.create(dict(prompt='生成产品视频',images=[],reference=None,duration=12.096,ratio='16:9',plan={'theme':'测试','segments':[{'duration':12.096}]},**extra))
    def test_optional_input_combinations(self):
        for text,image,video in itertools.product([False,True],repeat=3):
            data=dict(prompt='产品视频' if text else '',images=['img'] if image else [],reference='ref' if video else None)
            if text or image or video:self.assertIsInstance(NewJob(**data),NewJob)
            else:
                with self.assertRaises(ValidationError):NewJob(**data)
    def test_reference_only_and_image_defaults(self):
        store.put_asset(dict(id='ref',duration=12.096,width=864,height=496))
        j=intent.resolve(dict(prompt='',images=['img'],reference='ref',duration=None,ratio=None))
        self.assertEqual(j['prompt'],'')
        self.assertEqual(j['duration'],12.096)
        self.assertEqual(j['duration_mode'],'reference')
        self.assertEqual(j['ratio'],'16:9')
        self.assertIn('替换',j['inferred_intent'])
    def test_external_paths_and_symlinks_blocked(self):
        (self.root/'escape').symlink_to('/tmp')
        for name in ['../secret','/etc/passwd','.hypit/tasks.json','escape/out']:
            with self.assertRaises(ValueError):agent_tools.safe(self.root,name)
    def test_no_false_completion_or_stale_review(self):
        j=self.job();video=self.root/'v.mp4';video.write_bytes(b'actual version')
        for review in [dict(verdict='fail',sha256=agent_tools.digest(video)),dict(verdict='warn',sha256=agent_tools.digest(video)),dict(verdict='pass',sha256='old version')]:
            store.update(j['id'],final_review=review)
            with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools.media,'verify') as verify:
                with self.assertRaises(ValueError):agent_tools.dispatch(j['id'],'finish',dict(path='v.mp4'))
                verify.assert_not_called()
            self.assertNotEqual(store.get(j['id'])['status'],'completed')
    def test_reference_coverage_requires_all_intervals(self):
        self.assertFalse(agent_tools.reference_coverage([dict(start=0,end=4),dict(start=7,end=12)],12))
        self.assertTrue(agent_tools.reference_coverage([dict(start=6,end=12),dict(start=0,end=6)],12))
        self.assertFalse(agent_tools.reference_coverage([],12))
    def test_review_is_invalid_after_plan_changes(self):
        j=self.job();video=self.root/'v.mp4';video.write_bytes(b'generated')
        store.update(j['id'],final_review={'verdict':'pass','sha256':agent_tools.digest(video),'context_sha256':agent_tools.review_context(j)},plan={'theme':'不同内容','segments':[{'duration':12.096}]})
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools.media,'verify') as verify:
            with self.assertRaisesRegex(ValueError,'当前需求和方案'):agent_tools.dispatch(j['id'],'finish',dict(path='v.mp4'))
            verify.assert_not_called()
    def test_signature_tampering_and_expiry(self):
        name='a'*64+'.mp4';(self.root/'reference-blobs').mkdir();(self.root/'reference-blobs'/name).write_bytes(b'video')
        expires=int(time.time())+60;sig=media_gateway.signature(name,expires)
        self.assertEqual(media_gateway.reference(name,expires,sig).status_code,200)
        for e,s in [(expires,'bad'),(expires+1,sig),(int(time.time())-1,sig)]:
            with self.assertRaises(HTTPException):media_gateway.reference(name,e,s)
    def test_fractional_timeline(self):
        compiler.film_source(dict(ratio='16:9'),dict(segments=[{},{}]),self.root,[6,6.096])
        text=(self.root/'film.svml').read_text()
        self.assertIn('end="302f"',text)
        self.assertNotIn('end="300f"',text)
    def test_fractional_cuts_do_not_accumulate_rounding_error(self):
        compiler.film_source(dict(ratio='16:9'),dict(segments=[{}]*10),self.root,[.101]*10)
        self.assertIn('end="25f"',(self.root/'film.svml').read_text())
    def test_shutdown_preserves_resumable_state(self):
        import threading
        from app import agent_runner
        j=self.job();stop=threading.Event();stop.set()
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_runner,'wait_builds',side_effect=RuntimeError('服务正在停止')):
            agent_runner.process(j,stop)
        self.assertEqual(store.get(j['id'])['status'],'queued')
    def test_generated_sources_check_with_real_hypit(self):
        from PIL import Image
        j=self.job();root=agent_tools.prepare(j)
        Image.new('RGB',(128,128)).save(root/'assets/subject.jpg')
        for name,refs in [('text',{}),('image',{'images':['assets/subject.jpg']}),('frames',{'first_frame':'assets/subject.jpg','last_frame':'assets/subject.jpg'})]:
            result=agent_tools.dispatch(j['id'],'generation_source',{'name':name,'prompt':'暖色背景展示商品','duration':13,**refs})
            self.assertTrue(agent_tools.dispatch(j['id'],'check',{'run':result['run']})['ok'])
    def test_video_runtime_uses_autodl_wan_without_embedding_key(self):
        profile=hypit_adapter.setup(self.root)
        runtime=json.loads(profile.read_text())
        endpoint=runtime['endpoints']['autodl-wan']
        self.assertEqual(endpoint['config']['baseUrl'],config.VIDEO_BASE)
        self.assertEqual(endpoint['config']['apiKey'],{'store':'env','key':'AUTODL_API_KEY'})
        self.assertEqual(runtime['bindings']['@hypit/seedance@1#seedance-2'],'autodl-wan')
        self.assertNotIn('ARK_API_KEY',profile.read_text())
    def test_build_idempotence(self):
        j=self.job();source=self.root/'one.svml';run=self.root/'one.svrun'
        source.write_text('<import as="seedance" from="@hypit/seedance@1"/><seedance:TextVideo duration="13"/>')
        run.write_text('<svrun><author source="./one.svml"/><target output="clip.video"/></svrun>')
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools,'validate_project'),patch.object(agent_tools.hypit,'setup',return_value=hypit_adapter.setup(self.root)),patch.object(agent_tools.hypit,'plan'),patch.object(agent_tools.hypit,'build',return_value={'build':{'id':'build-1'}}) as build:
            first=agent_tools.dispatch(j['id'],'build',dict(run='one.svrun',output='clip.video'))
            second=agent_tools.dispatch(j['id'],'build',dict(run='one.svrun',output='clip.video'))
        self.assertEqual(first['build_id'],second['build_id']);build.assert_called_once()
        self.assertEqual(store.get(j['id'])['reserved_generation_seconds'],13)
    def test_uncertain_build_never_resubmits(self):
        j=self.job(agent_submission_intent={'stamp':'unknown'})
        run=self.root/'one.svrun';run.write_text('<svrun><target output="clip.video"/></svrun>')
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools,'validate_project'),patch.object(agent_tools.hypit,'setup'),patch.object(agent_tools.hypit,'plan'),patch.object(agent_tools.hypit,'build') as build:
            with self.assertRaises(ValueError):agent_tools.dispatch(j['id'],'build',dict(run='one.svrun',output='clip.video'))
            build.assert_not_called()
    def test_invented_output_is_rejected_before_submission(self):
        j=self.job();run=self.root/'one.svrun';run.write_text('<svrun><target output="final.video"/></svrun>')
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools,'validate_project'),patch.object(agent_tools.hypit,'build') as build:
            with self.assertRaisesRegex(ValueError,'actual run target'):agent_tools.dispatch(j['id'],'build',dict(run='one.svrun',output='final.mp4'))
            build.assert_not_called()
    def test_legacy_output_recovery_uses_actual_receipt(self):
        j=self.job(agent_builds={'stamp':{'build_id':'known','output':'invented.mp4','state':'complete','generation_seconds':4}})
        def collect(root,bid,output,target):
            self.assertEqual((bid,output),('known','clip.video'));target.write_bytes(b'generated result')
        with patch.object(agent_tools,'prepare',return_value=self.root),patch.object(agent_tools.hypit,'setup'),patch.object(agent_tools.hypit,'status',return_value={'build':{'targets':['clip.video']}}),patch.object(agent_tools.hypit,'get',side_effect=collect),patch.object(agent_tools.media,'probe',return_value={'format':{}}):
            agent_tools.dispatch(j['id'],'collect',dict(build_id='known',to='result.mp4'))
        self.assertEqual(store.get(j['id'])['agent_builds']['stamp']['output'],'clip.video')
    def test_generation_budget_duration_must_be_literal(self):
        p=self.root/'a.svml'
        p.write_text('<import as="video" from="@hypit/seedance@1"/><video:TextVideo duration="12"/>')
        self.assertEqual(agent_tools.generation_seconds([p]),12)
        for duration in ['{unbounded}','"60"']:
            p.write_text('<import as="video" from="@hypit/seedance@1"/><video:TextVideo duration='+duration+'/>')
            with self.assertRaises(ValueError):agent_tools.generation_seconds([p])
    def test_runtime_module_rejected(self):
        run=self.root/'a.svrun';run.write_text('<author source="a.svml"/>')
        (self.root/'a.svml').write_text('<import from="@hypit/program-space@1"/>')
        with self.assertRaises(ValueError):agent_tools.validate_project(self.root,run)
    def test_explicit_duration_cannot_be_silently_changed(self):
        j=self.job()
        with self.assertRaises(ValueError):store.reserve_plan(j['id'],{},15)

if __name__=='__main__':unittest.main()
