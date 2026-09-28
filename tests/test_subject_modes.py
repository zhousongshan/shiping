import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from app import subject_spec
from app.production_schema import fingerprint, file_hash


class SubjectModeTests(unittest.TestCase):
    def test_old_analysis_is_recomputed_and_modes_do_not_share_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'assets').mkdir()
            Image.new('RGB', (8, 8), 'yellow').save(root/'assets/chick.jpg')
            job = {'id': 'test', 'prompt': '户外跳舞，很可爱', 'images': ['chick']}
            inputs = [{'path': 'assets/chick.jpg', 'sha256': file_hash(root/'assets/chick.jpg')}]
            job['subject_spec'] = {'source_version': fingerprint({
                'inputs': inputs, 'goal': job['prompt'], 'feedback': None}),
                'assets': [{'path': 'assets/chick.jpg', 'role': 'other'}]}
            response = {'assets': [{'path': 'assets/chick.jpg', 'role': 'identity',
                                    'visible_traits': ['黄色']}], 'question': ''}
            with patch.object(subject_spec.planner, 'chat', return_value=response) as chat, \
                 patch.object(subject_spec.store, 'update'):
                creative = subject_spec.analyze(job, root)
                self.assertEqual(chat.call_count, 1)
                content = chat.call_args.args[1]
                self.assertEqual(json.loads(content[0]['text'])['mode'], 'create')
                self.assertEqual(content[2]['type'], 'image_url')
                job['subject_spec'] = creative
                self.assertEqual(subject_spec.analyze(job, root), creative)
                self.assertEqual(chat.call_count, 1)
                job['reference'] = 'reference-id'
                reference = subject_spec.analyze(job, root)
                self.assertEqual(chat.call_count, 2)
                self.assertEqual(json.loads(chat.call_args.args[1][0]['text'])['mode'], 'reference')
                self.assertNotEqual(creative['source_version'], reference['source_version'])


if __name__ == '__main__':
    unittest.main()
