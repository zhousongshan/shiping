"""Version 4 contract and recovery regressions. No paid or network requests."""
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config,store,intent,creative_brief as brief,unit_planner,production,workflows,pipeline,structured,subject_mapping
from app.production_schema import file_hash,context_version,judge,artifact


class CreativeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.data),patch.object(store,'DATA',self.data),
            patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':''})]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def job(self,*,prompt='做一个10秒新品介绍，只借鉴配色',images=False,reference=True):
        if reference:
            try:store.asset('ref')
            except KeyError:store.put_asset({'id':'ref','duration':30.74,'width':1080,'height':1920})
        job=store.create(intent.resolve({'prompt':prompt,'images':['img'] if images else [],
            'reference':'ref' if reference else None,'duration':None,'ratio':'9:16','workflow_version':4,'production_version':2}))
        root=self.data/'jobs'/job['id']/'project';(root/'assets').mkdir(parents=True)
        inputs=[];assets=[]
        if images:
            (root/'assets/img.jpg').write_bytes(b'image fixture')
            inputs=[{'path':'assets/img.jpg','sha256':file_hash(root/'assets/img.jpg')}]
            assets=[{'path':'assets/img.jpg','role':'identity','visible_traits':['粉色']}]
        fields={'subject_spec':{'version':'s','inputs':inputs,'assets':assets}}
        if reference:
            (root/'assets/reference.mp4').write_bytes(b'reference fixture')
            fields['reference_analysis']={'version':'r','status':'verified','source_sha256':file_hash(root/'assets/reference.mp4'),
                'duration':30.74,'candidate_boundaries':[15],'events':[{'id':'e1','second':1,'description':'开场'},
                {'id':'e2','second':25,'description':'结束'}]}
        return store.update(job['id'],**fields),root

    def response(self,job,mode=None):
        text=job['prompt']
        return {'goal':'创作新品介绍','reference_strategy':mode or ('adapt' if job.get('reference') else 'none'),
            'reference_quote':'','replacement_required':False,'target_seconds':10 if text else None,
            'duration_quote':text,'follow_reference':False,'question':'',
            'requirements':[{'id':'r1','description':'完整介绍主体','source':'user' if text else 'default',
                'evidence':text or '根据图片与参考形成展示安排'}]}

    def save_brief(self,job,root,response=None):
        with patch.object(brief.structured.planner,'chat',return_value=response or self.response(job)):
            brief.resolve(job,root)
        return store.get(job['id'])

    def plan(self,job):
        return {'theme':'新品','story':'完整介绍','style':'可爱','segments':[{'duration':10,'description':'介绍主体'}],
            'units':[{'id':'u1','duration':10,'description':'主体进入画面，展示后挥手告别','requirement_ids':['r1'],
                      'depends_on':[],'continuity':{'type':'independent','reason':'完整短场景'}}]}

    def test_text_duration_not_overridden_by_long_reference(self):
        job,root=self.job()
        self.assertIsNone(job['duration'])
        job=self.save_brief(job,root,self.response(job,'style'))
        self.assertEqual(job['duration'],10)
        self.assertEqual(job['creative_brief']['duration_source'],'text')
        plan=self.plan(job);unit_planner.validate(job,root,plan)
        self.assertNotIn('reference_range',plan['units'][0])

    def test_no_text_image_and_reference_is_valid_and_default_is_not_user_quote(self):
        job,root=self.job(prompt='',images=True)
        job=self.save_brief(job,root)
        self.assertIsNone(job['duration'])
        self.assertEqual(job['creative_brief']['reference_strategy'],'adapt')
        self.assertFalse(subject_mapping.required(job))
        bad=self.response(job);bad['requirements'][0]['source']='user'
        with self.assertRaisesRegex(ValueError,'实际原文'):brief.validate(job,bad)

    def test_recreate_requires_explicit_quote_and_full_coverage(self):
        job,root=self.job(prompt='逐镜复刻这个参考')
        reply=self.response(job,'recreate');reply.update(target_seconds=None,duration_quote='')
        with self.assertRaisesRegex(ValueError,'明确原文'):brief.validate(job,reply)
        reply['reference_quote']=job['prompt'];job=self.save_brief(job,root,reply)
        plan=self.plan(job);plan['units'][0].update(reference_range=[0,10],event_ids=['e1'])
        with self.assertRaisesRegex(ValueError,'遗漏|结尾'):unit_planner.validate(job,root,plan)

    def test_form_duration_conflict_can_be_resolved_by_user_feedback(self):
        job,root=self.job();job['requested_duration']=30
        reply=self.response(job)
        self.assertIn('question',brief.validate(job,reply))
        job['feedback']='覆盖表单，使用10秒';reply['form_override_quote']=job['feedback']
        self.assertEqual(brief.validate(job,reply)['target_seconds'],10)
        reply['form_override_quote']='不存在的回答'
        with self.assertRaises(ValueError):brief.validate(job,reply)

    def test_duration_number_must_match_the_actual_quote(self):
        job,root=self.job(prompt='做十秒介绍视频')
        reply=self.response(job)
        self.assertEqual(brief.validate(job,reply)['target_seconds'],10)
        reply['target_seconds']=30
        with self.assertRaisesRegex(ValueError,'不匹配'):brief.validate(job,reply)

    def test_changed_brief_invalidates_review_context(self):
        job,root=self.job();job=self.save_brief(job,root)
        original=context_version(job)
        changed=copy.deepcopy(job);changed['creative_brief']['requirements'][0]['description']='改为展示不同内容'
        self.assertNotEqual(context_version(changed),original)

    def test_missing_media_gateway_blocks_only_strict_reference_before_generation(self):
        job,root=self.job(prompt='逐镜复刻这个参考')
        reply=self.response(job,'recreate');reply.update(reference_quote=job['prompt'],target_seconds=None,duration_quote='')
        job=self.save_brief(job,root,reply)
        with patch.object(pipeline.agent_tools,'prepare',return_value=root),patch.object(production,'initialize'), \
                patch.object(config,'media_base_url',return_value=''),patch.object(pipeline.agent_tools,'dispatch') as tools:
            pipeline.run(job,threading.Event())
        self.assertEqual(store.get(job['id'])['status'],'needs_configuration')
        tools.assert_not_called()

    def test_missing_requirement_and_invented_event_rejected(self):
        job,root=self.job();job=self.save_brief(job,root)
        plan=self.plan(job);plan['units'][0]['requirement_ids']=[]
        with self.assertRaisesRegex(ValueError,'遗漏'):unit_planner.validate(job,root,plan)
        plan=self.plan(job);plan['units'][0]['event_ids']=['invented']
        with self.assertRaisesRegex(ValueError,'真实参考'):unit_planner.validate(job,root,plan)

    def test_style_does_not_force_subject_replacement(self):
        from app import subject_mapping
        job,root=self.job(images=True);job=self.save_brief(job,root,self.response(job,'style'))
        self.assertFalse(subject_mapping.required(job))
        invalid=self.response(job,'style');invalid['replacement_required']=True
        with self.assertRaises(ValueError):brief.validate(job,invalid)

    def test_generated_request_uses_brief_and_no_raw_reference_for_adaptation(self):
        job,root=self.job(images=True);job=self.save_brief(job,root)
        plan=self.plan(job);unit_planner.validate(job,root,plan)
        job=store.update(job['id'],plan=plan)
        def execute(jid,action,args):
            if action=='generation_source':
                with patch.object(production,'capabilities',return_value={'output_seconds':[4,15]}):
                    local={**job,'provider_profile':{'output_seconds':[4,15]}}
                    manifest=production.prepare_manifest(local,root,args)
                self.assertIn('统一创作要求',manifest['args']['prompt'])
                self.assertEqual(manifest['args'].get('videos',[]),[])
                return {}
            if action=='build':return {'state':'pending'}
            raise AssertionError(action)
        result=workflows.generate_unit(job,root,{'unit_id':'u1','prompt':'完整展示粉色主体的外观，在新的场景里缓慢转动后向观众挥手告别。'},execute)
        self.assertEqual(result['state'],'pending')

    def test_schema_repair_preserves_bad_response_and_stops_after_two(self):
        job,root=self.job(reference=False)
        bad={'verdict':'pass','checks':[{'status':'pass','evidence':None}]}
        good={'verdict':'pass','checks':[{'status':'pass','evidence':'实际0到2秒可见主体'}]}
        validate=lambda value:(judge(value),value)[1]
        with patch.object(structured.planner,'chat',side_effect=[bad,good]) as model:
            self.assertEqual(structured.call(root,'test-review','检查',[],validate,inspection=True),good)
            self.assertEqual(model.call_count,2)
        records=list((root/'production/test-review-errors').glob('*.json'))
        self.assertEqual(json.loads(records[0].read_text())['response'],bad)
        with patch.object(structured.planner,'chat',return_value=bad) as model:
            with self.assertRaises(structured.InspectionIncomplete):
                structured.call(root,'test-review','检查',[],validate,inspection=True)
            self.assertEqual(model.call_count,2)

    def test_inspection_error_keeps_task_recoverable_without_generation(self):
        job,root=self.job(reference=False);job=self.save_brief(job,root)
        plan=self.plan(job);unit_planner.validate(job,root,plan)
        job=store.update(job['id'],plan=plan,agent_builds={'one':{'build_id':'one','unit_id':'u1','state':'complete','collected_sha256':'clip'}})
        with patch.object(pipeline.agent_tools,'prepare',return_value=root),patch.object(production,'initialize'), \
                patch.object(pipeline.agent_tools,'dispatch',side_effect=structured.InspectionIncomplete('审片结构异常')) as tools:
            pipeline.run(job,threading.Event())
        self.assertEqual(store.get(job['id'])['status'],'needs_review')
        self.assertEqual([c.args[1] for c in tools.call_args_list],['collect_review'])

    def test_warn_retry_rechecks_once_without_regeneration(self):
        job,root=self.job(reference=False);job=self.save_brief(job,root)
        plan=self.plan(job);unit_planner.validate(job,root,plan)
        job=store.update(job['id'],plan=plan)
        store.update(job['id'],recheck_requested=True,agent_builds={'one':{'build_id':'one','unit_id':'u1','state':'complete','collected_sha256':'clip'}},
            unit_reviews={'u1':{'verdict':'warn','original_sha256':'clip','context_version':context_version(job),'summary':'证据不足'}})
        with patch.object(pipeline.agent_tools,'prepare',return_value=root),patch.object(production,'initialize'), \
                patch.object(pipeline.agent_tools,'dispatch',return_value={}) as tools:
            pipeline.run(store.get(job['id']),threading.Event())
        self.assertEqual([c.args[1] for c in tools.call_args_list],['collect_review'])
        self.assertEqual(store.get(job['id'])['status'],'needs_review')
        self.assertFalse(store.get(job['id'])['recheck_requested'])

    def test_review_must_cover_contract_requirements(self):
        job,root=self.job(reference=False);job=self.save_brief(job,root)
        with self.assertRaisesRegex(ValueError,'遗漏'):brief.validate_review(job,{'checks':[{'requirement_ids':[]}]})
        brief.validate_review(job,{'checks':[{'requirement_ids':['r1']}]})

    def test_legacy_context_hash_does_not_change_when_unused_brief_added(self):
        job,root=self.job(reference=False);job['workflow_version']=3
        old=context_version(job)
        self.assertEqual(context_version({**job,'creative_brief':{'version':'ignored'}}),old)

    def test_five_input_modes_reach_simulated_complete_delivery(self):
        # Real contract/plan/dependency code; generation, inspection and rendering are simulated.
        for text,images,reference in [(True,False,False),(True,True,False),(True,False,True),
                                      (True,True,True),(False,True,True)]:
            with self.subTest(text=text,images=images,reference=reference):
                job,root=self.job(prompt='制作10秒介绍视频' if text else '',images=images,reference=reference)
                actions=[]
                def model(instruction,content):
                    if instruction==brief.INSTRUCTION:return self.response(job)
                    return self.plan(store.get(job['id']))
                def dispatch(jid,action,args):
                    actions.append(action);current=store.get(jid)
                    if action=='set_plan':
                        unit_planner.validate(current,root,args);store.reserve_plan(jid,args,10);return {}
                    if action=='generate_unit':
                        clip=root/'clip.mp4';clip.write_bytes(b'synthetic generated output')
                        builds={'one':{'build_id':'one','unit_id':'u1','state':'complete','generation_seconds':10,
                            'collected_path':'clip.mp4','collected_sha256':file_hash(clip)}}
                        store.update(jid,agent_builds=builds);return builds['one']
                    if action=='collect_review':
                        review={'version':'adopted','verdict':'pass','path':'clip.mp4','sha256':file_hash(root/'clip.mp4'),
                            'original_sha256':file_hash(root/'clip.mp4'),'context_version':context_version(current),'dependencies':{}}
                        store.update(jid,unit_reviews={'u1':review});return review
                    if action=='compose_build':
                        record={'build_id':'film','state':'complete','generation_seconds':0}
                        store.update(jid,agent_builds={**current['agent_builds'],'film':record});return record
                    if action=='collect':
                        out=root/args['to'];out.parent.mkdir(exist_ok=True);out.write_bytes(b'synthetic complete output')
                        current['agent_builds']['film'].update(collected_path=args['to'],collected_sha256=file_hash(out))
                        store.update(jid,agent_builds=current['agent_builds']);return {}
                    if action=='review':
                        store.update(jid,final_review={'verdict':'pass','sha256':file_hash(root/args['path']),
                            'context_sha256':context_version(current)});return {}
                    if action=='finish':store.update(jid,status='completed');return {}
                    raise AssertionError(action)
                with patch.object(pipeline.agent_tools,'prepare',return_value=root),patch.object(production,'initialize'), \
                        patch.object(structured.planner,'chat',side_effect=model),patch.object(pipeline.agent_tools,'dispatch',side_effect=dispatch):
                    pipeline.run(job,threading.Event())
                final=store.get(job['id'])
                self.assertEqual(final['status'],'completed',final.get('error'))
                self.assertEqual(actions,['set_plan','generate_unit','collect_review','compose_build','collect','review','finish'])
                self.assertEqual(final['creative_brief']['reference_strategy'],'adapt' if reference else 'none')
