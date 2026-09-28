"""HTTP acceptance for optional input paths; worker disabled, no model calls."""
import io
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
from PIL import Image
from fastapi.testclient import TestClient
from app import config,store,media,worker
from app.server import app

class InputAPITests(unittest.TestCase):
    def test_all_combinations_and_upload_errors(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()
            with patch.object(config,'DATA',root),patch.object(store,'DATA',root),patch.object(media,'DATA',root),patch.object(worker,'start',return_value=MagicMock()):
                with TestClient(app) as client:
                    b=io.BytesIO();Image.new('RGB',(128,128),'yellow').save(b,format='PNG')
                    image=client.post('/api/assets?kind=image&name=test.png',content=b.getvalue()).json()['id']
                    video=root/'fixture.mp4'
                    media.command([config.FFMPEG,'-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x240:r=25:d=2','-c:v','libx264','-pix_fmt','yuv420p',str(video)])
                    uploaded=client.post('/api/assets?kind=video&name=test.mp4',content=video.read_bytes())
                    self.assertEqual(uploaded.status_code,200);reference=uploaded.json()['id']
                    for text,img,ref in itertools.product([False,True],repeat=3):
                        data={'prompt':'展示产品' if text else '', 'images':[image] if img else [],'reference':reference if ref else None}
                        r=client.post('/api/jobs',json=data)
                        self.assertEqual(r.status_code,200 if any([text,img,ref]) else 422)
                        if ref:self.assertEqual(r.json()['duration'],2)
                    self.assertEqual(client.post('/api/assets?kind=image',content=b'bad image').status_code,400)
                    self.assertEqual(client.post('/api/assets?kind=video',content=b'').status_code,400)
                    self.assertEqual(client.post('/api/jobs',json={'images':[image,image]}).status_code,400)
                    self.assertEqual(client.post('/api/jobs',json={'reference':image}).status_code,400)
                    self.assertEqual(client.post('/api/jobs',json={'prompt':'  '}).status_code,422)
                    self.assertEqual(client.post('/api/jobs',json={'prompt':'产品','ratio':'unknown'}).status_code,400)
                    limited=client.post('/api/jobs',json={'prompt':'展示小鸡','max_unit_generations':2})
                    self.assertEqual(limited.status_code,200)
                    self.assertEqual(store.get(limited.json()['id'])['max_unit_generations'],2)
                    for invalid in (0,4,True,1.5):
                        self.assertEqual(client.post('/api/jobs',json={'prompt':'展示小鸡','max_unit_generations':invalid}).status_code,422)
