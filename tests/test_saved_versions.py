import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import config,store,candidate
from app.server import app
from app.production_schema import file_hash


class SavedVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patches=[patch.object(config,'DATA',self.root),patch.object(store,'DATA',self.root),
                      patch.dict(os.environ,{'VIDEO_AGENT_DATABASE_URL':'','VIDEO_AGENT_EMBEDDED_WORKER':'0','VIDEO_AGENT_ENV':'local','VIDEO_AGENT_USERS_JSON':''})]
        for p in self.patches:p.start()
        self.job=store.create({'owner':'local','prompt':'dance','images':[],'engine':'hypit-agent-v5','duration':10})
        project=self.root/'jobs'/self.job['id']/'project';project.mkdir(parents=True)
        self.clip=project/'version.mp4';self.clip.write_bytes(b'saved video fixture')
        self.sha=file_hash(self.clip)
        self.build={'build_id':'build-one','unit_id':'u1','state':'complete','generation_seconds':10,'collected_path':'version.mp4'}
        store.update(self.job['id'],status='needs_attention',agent_builds={'one':self.build},generated_hashes=[self.sha])

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    def test_preview_available_on_failed_job_without_final_and_supports_range(self):
        with TestClient(app) as c:
            job=c.get('/api/jobs/'+self.job['id']).json()
            version=job['saved_clips'][0]
            self.assertEqual(version['quality_status'],'unchecked')
            response=c.get(version['preview_url']);self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,self.clip.read_bytes())
            part=c.get(version['preview_url'],headers={'Range':'bytes=0-4'})
            self.assertEqual(part.status_code,206);self.assertEqual(part.content,b'saved')
            self.assertEqual(c.get('/api/jobs/'+self.job['id']+'/video').status_code,404)
            self.assertEqual(c.get('/api/jobs/'+self.job['id']+'/versions/not-owned').status_code,404)

    def test_foreign_job_and_tampered_file_are_not_exposed(self):
        with TestClient(app) as c:
            store.update(self.job['id'],owner='someone-else')
            self.assertEqual(c.get('/api/jobs/'+self.job['id']+'/versions/build-one').status_code,404)
            store.update(self.job['id'],owner='local')
            self.clip.write_bytes(b'changed')
            self.assertEqual(c.get('/api/jobs/'+self.job['id']+'/versions/build-one').status_code,404)

    def test_two_alternative_versions_cannot_be_composed_as_two_scenes(self):
        store.update(self.job['id'],agent_builds={'one':self.build,'two':{**self.build,'build_id':'build-two'}})
        with patch.object(candidate.media,'probe',return_value={'format':{'duration':10},'streams':[]}),patch.object(candidate.media,'command') as render:
            with self.assertRaisesRegex(ValueError,'只能选择一个版本'):
                candidate.compose(store.get(self.job['id']),['build-one','build-two'])
            render.assert_not_called()
