"""Local FFmpeg/Hypit integration; no model calls or paid generation."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from app import config,store,agent_tools,media,hypit_adapter

class MediaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.ps=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root)]
        for p in self.ps:p.start()
        self.job=store.create(dict(prompt='测试拼接顺序',images=[],reference=None,duration=2,ratio='16:9',plan={'segments':[{'duration':1},{'duration':1}]}))
        self.project=agent_tools.prepare(self.job)
    def tearDown(self):
        for p in reversed(self.ps):p.stop()
        self.tmp.cleanup()
    def make(self,name,color,audio=False):
        path=self.project/'assets'/name
        cmd=[config.FFMPEG,'-v','error','-y','-f','lavfi','-i',f'color=c={color}:s=640x360:r=25:d=1']
        if audio:cmd+=['-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','aac']
        media.command([*cmd,'-c:v','libx264','-pix_fmt','yuv420p','-t','1',str(path)])
        return path
    def call(self,action,**args):return agent_tools.dispatch(self.job['id'],action,args)
    def test_reordering_does_not_overwrite_inputs_and_renders_silent_clip(self):
        red=self.make('clip1.mp4','red');blue=self.make('clip0.mp4','blue',True)
        hashes=[agent_tools.digest(red),agent_tools.digest(blue)]
        comp=self.call('compose',clips=[{'path':'assets/clip1.mp4','duration':1},{'path':'assets/clip0.mp4','duration':1}])
        self.assertEqual(hashes,[agent_tools.digest(red),agent_tools.digest(blue)])
        build=self.call('build',run=comp['run'],output=comp['output'])
        deadline=time.monotonic()+120
        while True:
            view=self.call('status',build_id=build['build_id'])['build'];outcome=view.get('work',{}).get('outcome') or view.get('result',{}).get('state')
            if outcome=='complete':break
            if view.get('work',{}).get('state')=='done':self.fail(str(view.get('failure') or outcome))
            if time.monotonic()>deadline:self.fail('Local render timed out')
            time.sleep(1)
        self.call('collect',build_id=build['build_id'],to='render.mp4')
        target=self.project/'render.mp4'
        media.verify(target,2)
        for at,channel in [(.2,0),(1.2,2)]:
            self.call('frame',path='render.mp4',at=at,to=f'frame-{channel}.png')
            px=Image.open(self.project/f'frame-{channel}.png').convert('RGB').getpixel((320,180))
            self.assertGreater(px[channel],180);self.assertLess(px[2-channel],50)
        self.call('cut',path='render.mp4',start=0,end=1,to='range.mp4');first=agent_tools.digest(self.project/'range.mp4')
        self.call('cut',path='render.mp4',start=1,end=2,to='range.mp4')
        self.assertNotEqual(first,agent_tools.digest(self.project/'range.mp4'))
        self.call('frame',path='render.mp4',at='last',to='last.png')
        self.assertGreater(Image.open(self.project/'last.png').getpixel((320,180))[2],180)
        # Changing density in the same cache must not reuse another timestamp's frame.
        sampled=media.frames(target,self.project/'samples',2)
        dense=media.frames(target,self.project/'samples',4)
        self.assertNotEqual(sampled[1][1],dense[1][1])
        px=Image.open(dense[1][1]).getpixel((320,180));self.assertGreater(px[0],180)
        # A local rendering of input footage is not proof that new video was generated.
        store.update(self.job['id'],final_review={'sha256':agent_tools.digest(target),'context_sha256':agent_tools.review_context(store.get(self.job['id'])),'verdict':'pass'})
        with self.assertRaisesRegex(ValueError,'No successful generated content'):self.call('finish',path='render.mp4')
        self.call('compose',clips=[{'path':'assets/clip1.mp4','duration':1},{'path':'assets/clip0.mp4','duration':1}],mute=True)
        self.assertNotIn('<audio:Item',(self.project/'film.svml').read_text())
        # Replacing media at the same path must invalidate its frame cache too.
        first=media.frames(red,self.project/'same-path-cache',1)[0][1]
        red.write_bytes(blue.read_bytes())
        second=media.frames(red,self.project/'same-path-cache',1)[0][1]
        self.assertNotEqual(first,second)
        self.assertGreater(Image.open(second).getpixel((320,180))[2],180)
    def test_long_plan_requires_units_and_complete_duration(self):
        store.update(self.job['id'],duration=16)
        with self.assertRaises(ValueError):self.call('set_plan',theme='长视频',story='展示',segments=[{'duration':16}])
        self.call('set_plan',theme='长视频',story='展示',segments=[{'duration':16}],units=[{'id':'a','duration':8,'continuity':{'type':'independent','reason':'开场'}},{'id':'b','duration':8,'continuity':{'type':'same_action','reason':'延续主体动作'}}])
        self.assertEqual(len(store.get(self.job['id'])['plan']['units']),2)
    def test_evidence_cannot_be_written_by_agent(self):
        with self.assertRaises(ValueError):self.call('write',path='evidence/fake-analysis.json',content='{}')
    def test_short_reference_is_adapted_without_modifying_original(self):
        source=self.make('short.mp4','red',True);before=agent_tools.digest(source)
        self.call('generation_source',name='adapted',prompt='展示原视频中的红色场景',duration=4,videos=['assets/short.mp4'])
        self.assertEqual(before,agent_tools.digest(source))
        adapted=list((self.project/'assets'/'adapted').glob('*.mp4'))
        self.assertEqual(len(adapted),1);media.verify(adapted[0],2)
        self.assertIn('assets/adapted/',(self.project/'adapted.svml').read_text())
    def test_join_failure_blocks_global_pass(self):
        # Exercise actual seam-review aggregation independently of subjective model quality.
        from app import planner
        clip=self.make('red.mp4','red');(self.project/'evidence').mkdir(exist_ok=True)
        store.update(self.job['id'],composition={'boundaries':[.5]},duration=1)
        observation={'audio':{'checked':False,'reason':'no audio track'}}
        with patch.object(agent_tools,'inspect_video',return_value=observation),patch.object(agent_tools,'review_joins',return_value=[{'at':.5,'verdict':'fail','issues':['主体变成另一只'],'summary':'身份变化'}]),patch.object(media,'frames',return_value=[]),patch.object(planner,'chat',return_value={'verdict':'pass','issues':[],'summary':'粗看通过'}):
            result=self.call('review',path='assets/red.mp4')
        self.assertEqual(result['verdict'],'fail')
        self.assertIn('未通过',result['summary'])
        with self.assertRaises(ValueError):self.call('finish',path='assets/red.mp4')
    def test_audio_join_failure_is_not_hidden_by_visual_pass(self):
        from app import planner
        clip=self.make('with-audio.mp4','blue',True)
        job={**self.job,'composition':{'boundaries':[.5]}}
        with patch.object(planner,'chat',side_effect=[{'verdict':'pass','issues':[],'summary':'画面连续'},{'verdict':'fail','issues':['拼接点爆音'],'summary':'声音跳变'}]) as chat:
            result=agent_tools.review_joins(self.project,clip,job)
        self.assertEqual(result[0]['verdict'],'fail')
        self.assertIn('拼接点爆音',result[0]['issues'])
        self.assertEqual(chat.call_args_list[1].args[1][1]['type'],'input_audio')

if __name__=='__main__':unittest.main()
