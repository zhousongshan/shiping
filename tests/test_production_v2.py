"""Business gates using isolated storage; no mock result is a visual acceptance claim."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from app import config, store, agent_tools, production, unit_planner, continuity, candidate, hypit_adapter
from app import media
from app.production_schema import file_hash, context_version, valid_time, judge


class ProductionV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.data),patch.object(store,'DATA',self.data)]
        for p in self.patches:p.start()
        self.job=store.create({'prompt':'用小鸡替换所有原角色','images':[],'reference':None,'ratio':'16:9','duration':8,
            'production_version':2,'engine':'hypit-agent-v5'})
        self.root=agent_tools.prepare(self.job)
        production.initialize(self.job,self.root)
        Image.new('RGB',(64,64),'yellow').save(self.root/'assets/chicken.jpg')
        subject={'version':'subject-v1','inputs':[{'path':'assets/chicken.jpg','sha256':file_hash(self.root/'assets/chicken.jpg')}],
            'assets':[{'path':'assets/chicken.jpg','role':'identity','visible_traits':['黄色小鸡']}],
            'replacement_rules':['完整替换原脸与身体，不只是换衣服']}
        store.update(self.job['id'],subject_spec=subject)
        self.plan={'theme':'小鸡','story':'玩耍','segments':[{'duration':4},{'duration':4}],
            'units':[{'id':'a','description':'小鸡进入画面挥手','duration':4,'depends_on':[],
                'continuity':{'type':'independent','reason':'开场'}},
                {'id':'b','description':'小鸡接着举起星星','duration':4,'depends_on':['a'],
                'continuity':{'type':'same_action','reason':'接着挥手后举星星'}}]}
        unit_planner.validate(self.fresh(),self.root,self.plan)
        store.update(self.job['id'],plan=self.plan)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def fresh(self):return store.get(self.job['id'])

    def args(self,unit_id='a'):
        return {'unit_id':unit_id,'name':'first','duration':4,'images':['assets/chicken.jpg'],
            'prompt':'完整黄色毛绒小鸡走进星星场景，举起右手打招呼，保留图片里的脸部与轮廓。'}

    def accept_a(self):
        # Deliberately synthetic fixture tests dependency validation, not vision quality.
        p=self.root/'first.mp4';p.write_bytes(b'test-output')
        Image.new('RGB',(64,64),'yellow').save(self.root/'assets/end.jpg')
        r={'version':'review-1','verdict':'pass','path':'first.mp4','sha256':file_hash(p),
            'context_version':context_version(self.fresh()),'dependencies':{},'end_frame':'assets/end.jpg',
            'end_frame_sha256':file_hash(self.root/'assets/end.jpg'),'usable_range':[0,4]}
        store.update(self.job['id'],unit_reviews={'a':r});return r

    def test_continuity_label_without_dependency_is_rejected(self):
        plan=copy.deepcopy(self.plan);plan['units'][1]['depends_on']=[]
        with self.assertRaisesRegex(ValueError,'依赖前一'):
            unit_planner.validate(self.fresh(),self.root,plan)

    def test_standard_short_film_does_not_pay_for_each_narrative_beat(self):
        job={**self.fresh(),'workflow_version':3}
        with self.assertRaisesRegex(ValueError,'一个生成单元'):
            unit_planner.validate(job,self.root,copy.deepcopy(self.plan))
        merged=copy.deepcopy(self.plan)
        merged['units']= [{**merged['units'][0],'duration':8,'description':'小鸡进入画面挥手，然后接着举起星星'}]
        unit_planner.validate(job,self.root,merged)

    def test_unreviewed_previous_take_blocks_generation(self):
        with self.assertRaisesRegex(ValueError,'尚未通过'):
            production.prepare_manifest(self.fresh(),self.root,self.args('b'))

    def test_accepted_previous_take_must_actually_be_attached(self):
        self.accept_a()
        with self.assertRaisesRegex(ValueError,'没有绑定'):
            production.prepare_manifest(self.fresh(),self.root,self.args('b'))
        args=self.args('b');args['images'].append('assets/end.jpg')
        self.assertEqual(production.prepare_manifest(self.fresh(),self.root,args)['dependencies'],{'a':'review-1'})

    def test_changed_frame_and_identity_are_rejected(self):
        self.accept_a();args=self.args('b');args['images'].append('assets/end.jpg')
        (self.root/'assets/end.jpg').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'衔接画面'):
            production.prepare_manifest(self.fresh(),self.root,args)
        (self.root/'assets/chicken.jpg').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'素材已变化'):
            production.prepare_manifest(self.fresh(),self.root,self.args())

    def test_real_source_manifest_and_tamper_detection(self):
        created=agent_tools.dispatch(self.job['id'],'generation_source',self.args())
        record=production.gate_build(self.fresh(),self.root,created['run'],created['output'])
        self.assertEqual(record['unit_id'],'a')
        self.assertIn('完整替换原脸',record['args']['prompt'])
        p=self.root/'first.svml';p.write_text(p.read_text().replace('chicken.jpg','other.jpg'))
        with self.assertRaisesRegex(ValueError,'manifest'):
            production.gate_build(self.fresh(),self.root,created['run'],created['output'])

    def test_handwritten_source_cannot_submit(self):
        (self.root/'rogue.svml').write_text('<import as="seedance" from="@hypit/seedance@1"/><seedance:TextVideo duration="4"/>')
        (self.root/'rogue.svrun').write_text('<svrun><author source="rogue.svml"/><target output="clip.video"/></svrun>')
        with patch.object(agent_tools,'validate_project'),patch.object(agent_tools.hypit,'plan'),patch.object(agent_tools.hypit,'build') as submit:
            with self.assertRaisesRegex(ValueError,'manifest'):
                agent_tools.dispatch(self.job['id'],'build',{'run':'rogue.svrun','output':'clip.video'})
            submit.assert_not_called()

    def test_no_repeated_generation_without_failed_review(self):
        store.update(self.job['id'],agent_builds={'one':{'unit_id':'a','state':'complete','generation_seconds':4}})
        with self.assertRaisesRegex(ValueError,'未明确'):
            production.prepare_manifest(self.fresh(),self.root,self.args())

    def test_case_limit_blocks_manifest_and_direct_build(self):
        made=agent_tools.dispatch(self.job['id'],'generation_source',self.args())
        builds={str(i):{'unit_id':'a','state':'failed','generation_seconds':4} for i in range(2)}
        store.update(self.job['id'],max_unit_generations=2,agent_builds=builds)
        with self.assertRaisesRegex(ValueError,'次数上限'):
            production.prepare_manifest(self.fresh(),self.root,self.args())
        with patch.object(agent_tools,'validate_project'),patch.object(hypit_adapter,'plan'),patch.object(hypit_adapter,'build') as submit:
            with self.assertRaisesRegex(ValueError,'次数上限'):
                agent_tools.dispatch(self.job['id'],'build',{'run':made['run'],'output':made['output']})
            submit.assert_not_called()

    def test_changed_accepted_dependency_requires_a_new_dependent_take(self):
        self.accept_a()
        args=self.args('b');args['images'].append('assets/end.jpg')
        store.update(self.job['id'],agent_builds={'old':{'unit_id':'b','state':'complete','generation_seconds':4,'manifest_id':'old-manifest'}},
                     generation_manifests={'old-manifest':{'dependencies':{'a':'old-review'}}})
        manifest=production.prepare_manifest(self.fresh(),self.root,args)
        self.assertEqual(manifest['dependencies'],{'a':'review-1'})
        store.update(self.job['id'],generation_manifests={'old-manifest':{'dependencies':{'a':'review-1'}}})
        with self.assertRaisesRegex(ValueError,'未明确'):
            production.prepare_manifest(self.fresh(),self.root,args)

    def test_context_and_dependency_versions_invalidate_review(self):
        self.accept_a();self.assertEqual(continuity.accepted(self.fresh(),self.root,'a')['version'],'review-1')
        store.update(self.job['id'],prompt='另一条要求')
        with self.assertRaisesRegex(ValueError,'过期'):
            continuity.accepted(self.fresh(),self.root,'a')

    def test_reference_event_omission_and_equal_split_are_rejected(self):
        (self.root/'assets/reference.mp4').write_bytes(b'ref')
        analysis={'source_sha256':file_hash(self.root/'assets/reference.mp4'),'status':'verified','version':'ref-v1',
            'candidate_boundaries':[3], 'duration':8, 'events':[{'id':'e1','second':1},{'id':'e2','second':5},{'id':'e3','second':7}]}
        store.update(self.job['id'],reference='ref',reference_analysis=analysis)
        plan=copy.deepcopy(self.plan)
        plan['units'][0].update(event_ids=['e1'],reference_range=[0,4])
        plan['units'][1].update(event_ids=['e2'],reference_range=[4,8])
        with self.assertRaisesRegex(ValueError,'非候选'):
            unit_planner.validate(self.fresh(),self.root,plan)
        plan['units'][1]['continuity']['boundary_reason']='同一镜头内动作阶段拆分'
        with self.assertRaisesRegex(ValueError,'遗漏'):
            unit_planner.validate(self.fresh(),self.root,plan)
        plan['units'][1]['event_ids'].append('e3')
        unit_planner.validate(self.fresh(),self.root,plan)

    def test_approximate_and_strict_timing_are_separate(self):
        self.assertFalse(valid_time({'duration':30},30.21))
        self.assertTrue(valid_time({'duration':30,'timing_policy':{'mode':'approximate','min':29,'max':31}},30.21))
        self.assertFalse(valid_time({'duration':30,'timing_policy':{'mode':'approximate','min':29,'max':31}},32))

    def test_provider_is_pinned_and_snapshot_immutable(self):
        original=hypit_adapter.setup(self.root);doc=json.loads(original.read_text())
        snap=hypit_adapter.snapshot(self.root,original)
        with patch.object(config,'VIDEO_BASE','https://different.invalid'):
            self.assertEqual(json.loads(hypit_adapter.setup(self.root).read_text())['bindings'],doc['bindings'])
            self.assertEqual(production.capabilities(self.root)['base_url'],doc['endpoints']['autodl-wan']['config']['baseUrl'])
        original.write_text('{}')
        self.assertEqual(json.loads(snap.read_text()),doc)

    def test_forged_review_pass_cannot_override_failed_evidence(self):
        self.assertEqual(judge({'verdict':'pass','checks':[{'status':'fail','evidence':'2秒出现原玩偶脸'}]}),'fail')
        with self.assertRaises(ValueError):judge({'verdict':'pass','checks':[]})

    def test_production_and_runtime_records_are_not_agent_editable(self):
        for path in ['production/manifests/fake.json','hypit-runtime-other.json','reference_unit1.cut.json']:
            with self.assertRaises(ValueError):agent_tools.dispatch(self.job['id'],'write',{'path':path,'content':'{}'})

    def test_candidate_rejects_foreign_and_unrecorded_clips(self):
        store.update(self.job['id'],status='needs_attention')
        with self.assertRaises(ValueError):candidate.compose(self.fresh(),['foreign-build'])

    def test_real_jpeg_frame_export_works_with_current_ffmpeg(self):
        video=self.root/'sample.mp4'
        media.command([config.FFMPEG,'-v','error','-y','-f','lavfi','-i','color=yellow:s=64x64:r=24:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(video)])
        agent_tools.dispatch(self.job['id'],'frame',{'path':'sample.mp4','at':'last','to':'last.jpg'})
        with Image.open(self.root/'last.jpg') as im:self.assertEqual(im.size,(64,64))

    def test_uncertain_submission_preserves_unit_and_manifest_for_recovery(self):
        made=agent_tools.dispatch(self.job['id'],'generation_source',self.args())
        with patch.object(agent_tools,'validate_project'),patch.object(hypit_adapter,'plan'),patch.object(hypit_adapter,'build',side_effect=RuntimeError('lost local receipt')):
            with self.assertRaisesRegex(RuntimeError,'lost'):
                agent_tools.dispatch(self.job['id'],'build',{'run':made['run'],'output':made['output']})
        intent=self.fresh()['agent_submission_intent']
        self.assertEqual(intent['record']['unit_id'],'a')
        self.assertIn(intent['record']['manifest_id'],self.fresh()['generation_manifests'])
        self.assertEqual(intent['record']['generation_seconds'],4)

    def test_composition_keeps_accepted_paths_and_records_actual_inputs(self):
        a=self.accept_a();b=copy.deepcopy(a);b.update(version='review-2',dependencies={'a':'review-1'},path='second.mp4')
        (self.root/'second.mp4').write_bytes(b'second-output');b['sha256']=file_hash(self.root/'second.mp4')
        store.update(self.job['id'],unit_reviews={'a':a,'b':b})
        clips=[{'unit_id':'a','path':'first.mp4','duration':4},{'unit_id':'b','path':'second.mp4','duration':4}]
        with patch.object(media,'probe',return_value={'format':{'duration':'4'},'streams':[{'codec_type':'video'}]}):
            result=agent_tools.dispatch(self.job['id'],'compose',{'clips':clips})
        composed=self.fresh()['composition']
        self.assertEqual([c['path'] for c in composed['clips']],['first.mp4','second.mp4'])
        production.validate_composition(self.fresh(),self.root,composed['clips'])
        self.assertIn('film.svml',composed['sources'])
        with patch.object(agent_tools,'validate_project'),patch.object(hypit_adapter,'plan'),patch.object(hypit_adapter,'build',return_value={'build':{'id':'local-render'}}):
            built=agent_tools.dispatch(self.job['id'],'build',{'run':result['run'],'output':result['output']})
        self.assertEqual(built['generation_seconds'],0)


if __name__=='__main__':unittest.main()
