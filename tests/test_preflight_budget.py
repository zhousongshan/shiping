"""No paid calls. Whole-film quota, signed-download and terminal-retry regressions."""
import copy
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch,Mock
from urllib.parse import urlsplit,parse_qs
from app import config,store,plan_budget,media_preflight,failed_generation,production,recovery,errors,workflows
from app.production_schema import context_version


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root),
            patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':'','VIDEO_AGENT_JOB_SECONDS':'50',
                'VIDEO_AGENT_DAILY_GENERATION_SECONDS':'100','VIDEO_AGENT_REMOTE_CONCURRENT':'10'})]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def job(self,durations=(10,10),**extra):
        return store.create({'workflow_version':4,'duration':sum(durations),'owner':'local',
            'plan':{'units':[{'id':f'u{i+1}','duration':d} for i,d in enumerate(durations)]},**extra})

    def intent(self,uid,seconds=10,**extra):
        return {'stamp':'stamp','record':{'unit_id':uid,'generation_seconds':seconds,**extra}}

    def failed_job(self):
        job=self.job((10,10));root=self.root/'jobs'/job['id']/'project';root.mkdir(parents=True)
        (root/'runtime.json').write_text('{}')
        record={'unit_id':'u1','build_id':'b1','state':'failed','generation_seconds':10,
            'provider_task_id':'task1','runtime_profile':'runtime.json','run':'old.svrun','output':'clip.video'}
        return store.update(job['id'],status='needs_attention',agent_builds={'old':record},reserved_generation_seconds=10)

    def proof(self,job):
        return {'build_id':'b1','provider_task_id':'task1','status':'FAILED','checked_at':time.time()}

    def test_full_plan_is_rounded_before_any_submission(self):
        job=self.job((2.1,14.2,1.1))
        self.assertEqual(plan_budget.costs(job),{'u1':4,'u2':15,'u3':4})
        with patch.dict(os.environ,{'VIDEO_AGENT_JOB_SECONDS':'22'}):
            with self.assertRaisesRegex(plan_budget.BudgetUnavailable,'整片'):store.ensure_plan_budget(job['id'])
        self.assertNotIn('agent_submission_intent',store.get(job['id']))
        self.assertEqual(store.ensure_plan_budget(job['id'])['remaining_seconds'],23)

    def test_two_jobs_cannot_reserve_same_daily_capacity_and_hold_is_consumed_once(self):
        a=self.job();b=self.job()
        with patch.dict(os.environ,{'VIDEO_AGENT_DAILY_GENERATION_SECONDS':'30'}):
            store.ensure_plan_budget(a['id'])
            with self.assertRaises(plan_budget.BudgetUnavailable):store.ensure_plan_budget(b['id'])
            store.reserve_submission(a['id'],self.intent('u1'),10,50)
            saved=store.get(a['id'])
            self.assertEqual(saved['plan_budget']['remaining_seconds'],10)
            self.assertEqual(saved['reserved_generation_seconds'],10)
            store.update(a['id'],agent_submission_intent=None,agent_builds={'a':{'unit_id':'u1','state':'complete','generation_seconds':10}})
            with self.assertRaises(plan_budget.BudgetUnavailable):store.ensure_plan_budget(b['id'])
            self.assertEqual(store.ensure_plan_budget(a['id'])['remaining_seconds'],10)
            store.reserve_submission(a['id'],self.intent('u2'),10,50)
            self.assertEqual(store.get(a['id'])['plan_budget']['remaining_seconds'],0)

    def test_repair_cannot_spend_budget_for_unfinished_units(self):
        job=self.job()
        job=store.update(job['id'],reserved_generation_seconds=10,agent_builds={'one':{'unit_id':'u1','generation_seconds':10,'state':'complete'}})
        with self.assertRaises(plan_budget.BudgetUnavailable):
            store.reserve_submission(job['id'],self.intent('u1'),10,25)
        self.assertNotIn('agent_submission_intent',store.get(job['id']))

    def test_legacy_submission_respects_other_jobs_hold(self):
        a=self.job();b=store.create({'owner':'local'})
        with patch.dict(os.environ,{'VIDEO_AGENT_DAILY_GENERATION_SECONDS':'22'}):
            store.ensure_plan_budget(a['id'])
            with self.assertRaisesRegex(ValueError,'今日预留'):store.reserve_submission(b['id'],self.intent('x',4),4,50)

    def test_expired_hold_is_rechecked_and_not_an_unlimited_authorization(self):
        a=self.job();b=self.job()
        with patch.dict(os.environ,{'VIDEO_AGENT_DAILY_GENERATION_SECONDS':'20'}):
            hold=store.ensure_plan_budget(a['id']);hold['expires']=time.time()-1
            store.update(a['id'],plan_budget=hold);store.ensure_plan_budget(b['id'])
            with self.assertRaises(plan_budget.BudgetUnavailable):store.reserve_submission(a['id'],self.intent('u1'),10,50)

    def test_capacity_wait_does_not_consume_hold(self):
        a=self.job();b=self.job();store.ensure_plan_budget(b['id'])
        with patch.dict(os.environ,{'VIDEO_AGENT_REMOTE_CONCURRENT':'1'}):
            store.reserve_submission(a['id'],self.intent('u1'),10,50)
            self.assertFalse(store.reserve_submission(b['id'],self.intent('u1'),10,50))
        self.assertEqual(store.get(b['id'])['plan_budget']['remaining_seconds'],20)
        self.assertNotIn('reserved_generation_seconds',store.get(b['id']))

    def test_confirmed_failure_grants_one_stable_new_attempt_without_erasing_spend(self):
        job=self.failed_job()
        with patch.object(failed_generation.provider_recovery,'poll',return_value={'status':'FAILED'}) as poll:
            proof=failed_generation.verify(job,'b1')
        poll.assert_called_once()
        job=store.authorize_failed_generation(job['id'],proof)
        grant=job['failed_generation_retries']['b1']
        self.assertEqual(job['reserved_generation_seconds'],10)
        self.assertEqual(job['status'],'queued')
        with self.assertRaises(ValueError):store.authorize_failed_generation(job['id'],proof)
        self.assertTrue(store.reserve_submission(job['id'],self.intent('u1',retry_token=grant['token'],retry_of='b1'),10,50))
        saved=store.get(job['id']);self.assertEqual(saved['reserved_generation_seconds'],20)
        self.assertTrue(saved['failed_generation_retries']['b1']['consumed_at'])
        self.assertEqual(saved['agent_builds']['old']['build_id'],'b1')

    def test_remote_success_pending_unknown_and_no_receipt_never_authorize(self):
        for status in ('SUCCEEDED','RUNNING','PENDING'):
            job=self.failed_job()
            with patch.object(failed_generation.provider_recovery,'poll',return_value={'status':status}):
                with self.assertRaises(ValueError):failed_generation.verify(job,'b1')
        for field in ('agent_submission_intent','submission_uncertain','submission_intent'):
            job=self.failed_job();job=store.update(job['id'],**{field:{'id':'unknown'}})
            with patch.object(failed_generation.provider_recovery,'poll') as poll:
                with self.assertRaises(ValueError):failed_generation.verify(job,'b1')
                poll.assert_not_called()

    def test_failed_build_cannot_bypass_authorization_via_new_source(self):
        job=self.failed_job()
        with self.assertRaisesRegex(ValueError,'新版本'):failed_generation.require_authorization(job,'u1')
        with self.assertRaisesRegex(ValueError,'新版本'):store.reserve_submission(job['id'],self.intent('u1'),10,50)
        job=store.authorize_failed_generation(job['id'],self.proof(job))
        with self.assertRaisesRegex(ValueError,'绑定'):store.reserve_submission(job['id'],self.intent('u1'),10,50)
        changed=copy.deepcopy(job);changed['prompt']='changed'
        with self.assertRaises(ValueError):failed_generation.require_authorization(changed,'u1')

    def test_confirmed_failed_history_is_not_repolled_when_resuming_new_revision(self):
        job=self.failed_job();job=store.authorize_failed_generation(job['id'],self.proof(job))
        with patch.object(recovery.hypit,'status') as poll:
            self.assertFalse(recovery.wait_builds(job,threading.Event(),once=True));poll.assert_not_called()

    def test_business_failure_does_not_clear_acceptance_uncertainty(self):
        failure=errors.classify('AutoDL HTTP 500 insufficient account balance',stage='generation',submission=True)
        self.assertTrue(failure.submission_uncertain)
        self.assertFalse(failure.retryable)
        self.assertEqual(failure.reason_code,'INSUFFICIENT_BALANCE')

    def test_actual_signed_get_checks_full_bytes_and_rejects_html_or_redirect(self):
        root=self.root/'project';root.mkdir();source=root/'ref.mp4';source.write_bytes(b'video bytes'*100)
        blobs=self.root/'blobs';blobs.mkdir();secret=self.root/'secret';secret.write_bytes(b'test-signing-secret')
        class Handler(BaseHTTPRequestHandler):
            mode='ok'
            def do_GET(self):
                split=urlsplit(self.path);name=split.path.rsplit('/',1)[-1];query=parse_qs(split.query)
                expected=hmac.new(secret.read_bytes(),f"{name}:{query['expires'][0]}".encode(),hashlib.sha256).hexdigest()
                if query['signature_value'][0]!=expected:self.send_error(403);return
                if self.mode=='redirect':self.send_response(302);self.send_header('Location','/login');self.end_headers();return
                data=(blobs/name).read_bytes() if self.mode=='ok' else b'<html>login</html>'
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        profile=root/'runtime.json';profile.write_text(json.dumps({'bindings':{'@hypit/seedance@1#seedance-2':'wan'},'endpoints':{'wan':{'config':{
            'mediaBaseUrl':f'http://127.0.0.1:{server.server_port}','referenceDirectory':str(blobs),'mediaSecretFile':str(secret)}}}}))
        try:
            record=media_preflight.verify(root,[source],profile)
            self.assertEqual(record['status'],'passed');self.assertEqual(record['files'][0]['bytes'],source.stat().st_size)
            self.assertNotIn('signature_value',json.dumps(record))
            for mode in ('html','redirect'):
                Handler.mode=mode
                with self.assertRaises(media_preflight.MediaUnavailable):media_preflight.verify(root,[source],profile)
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(len(list((root/'production/media-preflight').glob('*.json'))),3)

    def test_identity_reference_is_separate_from_action_continuity_and_hash_checked(self):
        from app import continuity
        job=self.job();root=self.root/'identity';root.mkdir()
        (root/'clip.mp4').write_bytes(b'clip');(root/'identity.jpg').write_bytes(b'identity');(root/'end.jpg').write_bytes(b'end')
        from app.production_schema import file_hash
        spec={'id':'u2','identity_depends_on':['u1'],'depends_on':[],'continuity':{'type':'new_scene'},'subject_paths':[]}
        job.update(subject_spec={'inputs':[],'assets':[]})
        job['unit_reviews']={'u1':{'verdict':'pass','version':'v1','path':'clip.mp4','sha256':file_hash(root/'clip.mp4'),
            'context_version':context_version(job),'identity_frame':'identity.jpg','identity_frame_sha256':file_hash(root/'identity.jpg'),
            'end_frame':'end.jpg','end_frame_sha256':file_hash(root/'end.jpg')}}
        with self.assertRaises(ValueError):continuity.validate_inputs(job,root,spec,{'images':['end.jpg']})
        self.assertEqual(continuity.validate_inputs(job,root,spec,{'images':['identity.jpg']}),{'u1':'v1'})
        (root/'identity.jpg').write_bytes(b'changed')
        with self.assertRaises(ValueError):continuity.validate_inputs(job,root,spec,{'images':['identity.jpg']})

    def test_cleanup_preserves_live_signed_copies_and_all_job_originals(self):
        from scripts import cleanup_media
        blobs=self.root/'reference-blobs';blobs.mkdir()
        old=blobs/('a'*64+'.mp4');old.write_bytes(b'old');os.utime(old,(0,0))
        new=blobs/('b'*64+'.mp4');new.write_bytes(b'new')
        original=self.root/'original.mp4';original.write_bytes(b'original');os.utime(original,(0,0))
        self.assertEqual(cleanup_media.cleanup()['files'],1);self.assertTrue(old.exists())
        with patch.object(runtime:=cleanup_media.runtime,'worker_alive',return_value=False):
            cleanup_media.cleanup(apply=True)
        self.assertFalse(old.exists());self.assertTrue(new.exists());self.assertTrue(original.exists())
        with self.assertRaises(ValueError):cleanup_media.cleanup(retention_days=1)

    def test_process_identity_rejects_reused_pid(self):
        from app import local_process
        saved={'pid':123,'identity':'Monday Python service'}
        with patch.object(local_process,'identity',return_value='Tuesday Python other service'):
            self.assertFalse(local_process.matches(saved))
        with patch.object(local_process,'identity',return_value=saved['identity']):
            self.assertTrue(local_process.matches(saved))

    def test_actual_build_stops_before_submit_when_media_preflight_fails(self):
        from app import agent_tools
        job=self.job((10,),production_version=2)
        root=self.root/'project';root.mkdir();run=root/'unit.svrun';run.write_text('<svrun><target output="clip.video"/></svrun>')
        profile=root/'runtime.json';profile.write_text('{}')
        with patch.object(agent_tools,'prepare',return_value=root),patch.object(agent_tools,'validate_project'), \
             patch.object(agent_tools.hypit,'setup',return_value=profile),patch.object(agent_tools.hypit,'snapshot',return_value=profile), \
             patch.object(agent_tools.hypit,'plan',return_value={}),patch.object(agent_tools,'source_dependencies',return_value=[run]), \
             patch.object(agent_tools,'build_stamp',return_value='stamp'),patch.object(agent_tools,'generation_seconds',return_value=10), \
             patch.object(production,'gate_build',return_value={'unit_id':'u1','version':'manifest'}), \
             patch.object(media_preflight,'verify',side_effect=media_preflight.MediaUnavailable('unreachable')), \
             patch.object(agent_tools.hypit,'build') as submit:
            with self.assertRaises(media_preflight.MediaUnavailable):agent_tools.dispatch(job['id'],'build',{'run':'unit.svrun','output':'clip.video'})
            submit.assert_not_called()
        self.assertNotIn('agent_submission_intent',store.get(job['id']))
        self.assertNotIn('reserved_generation_seconds',store.get(job['id']))

    def test_failed_revision_uses_one_stable_request_identity_and_reuses_source(self):
        job=self.failed_job();job=store.authorize_failed_generation(job['id'],self.proof(job))
        root=self.root/'jobs'/job['id']/'project'
        job.update(production_version=2,reference=None)
        requests=[]
        def execute(jid,action,args):
            if action=='generation_source':
                requests.append(copy.deepcopy(args));(root/(args['name']+'.svrun')).write_text('saved')
                job['generation_manifests']={'m':{'run':args['name']+'.svrun'}}
                return {}
            if action=='build':return {'run':args['run'],'state':'pending'}
            raise AssertionError(action)
        args={'unit_id':'u1','prompt':'完整展示同一个主体的新版本，保持场景动作和原有要求。'}
        first=workflows.generate_unit(job,root,args,execute)
        second=workflows.generate_unit(job,root,args,execute)
        self.assertEqual(first,second);self.assertEqual(len(requests),1)
        self.assertIn(job['failed_generation_retries']['b1']['token'],requests[0]['prompt'])
        self.assertEqual(args['prompt'],'完整展示同一个主体的新版本，保持场景动作和原有要求。')

    def test_full_film_music_preserves_original_tracks_and_spans_silent_segments(self):
        from app import film_audio,media
        directory=self.root/'music-library';directory.mkdir()
        music=directory/'music.wav'
        media.command([config.FFMPEG,'-v','error','-y','-f','lavfi','-i','sine=frequency=220:duration=1',str(music)])
        catalog=[{'id':'calm','file':'music.wav','description':'测试音调','license_note':'test fixture generated locally',
            'approved_for_company_use':True,'allow_loop':True}]
        (directory/'catalog.json').write_text(json.dumps(catalog))
        root=self.root/'film';root.mkdir()
        clips=[]
        for index,audio in enumerate((True,False)):
            target=root/f'clip{index}.mp4'
            command=[config.FFMPEG,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=128x128:r=25:d=1']
            if audio:command+=['-f','lavfi','-i','sine=frequency=880:duration=1','-c:a','aac']
            media.command([*command,'-c:v','libx264','-t','1',str(target)])
            clips.append({'path':target.name,'duration':1})
        plan={'audio_plan':{'mode':'library','music_id':'calm'}};film_audio.validate(plan)
        job={'plan':plan,'duration':2}
        result=film_audio.compose(job,root,clips);path=root/result['audio']
        self.assertAlmostEqual(float(media.probe(path)['format']['duration']),2,places=1)
        self.assertEqual(film_audio.compose(job,root,clips),result)
        # Measure actual mixed energy in the original+music and music-only portions.
        import wave,array
        with wave.open(str(path)) as wav:
            rate=wav.getframerate();channels=wav.getnchannels();samples=array.array('h',wav.readframes(wav.getnframes()))
        rms=lambda at:sum(x*x for x in samples[int(at*rate)*channels:int((at+.2)*rate)*channels])/int(.2*rate*channels)
        self.assertGreater(rms(.3),rms(1.3)*2)
        self.assertGreater(rms(1.3),0)
        music.write_bytes(b'changed')
        with self.assertRaises(film_audio.AudioUnavailable):film_audio.preflight(job)
