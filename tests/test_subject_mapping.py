import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import subject_mapping,production

class SubjectMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.job={'id':'isolated','workflow_version':3,'production_version':2,'reference':'video','prompt':'',
            'subject_spec':{'version':'s','assets':[{'role':'identity','path':'chick.jpg'}]},
            'reference_analysis':{'version':'r','status':'verified','events':[]},
            'plan':{'production_version':2,'units':[{'id':'u1'}]}}
    def tearDown(self):self.temp.cleanup()

    def resolve(self,result,job=None):
        with patch.object(subject_mapping.planner,'chat',return_value=result),patch.object(subject_mapping.store,'update'):
            return subject_mapping.resolve(job or self.job,self.root)

    def test_ambiguous_multisubject_input_never_authorizes_replace_all(self):
        mapping=self.resolve({'subjects':['wand toy','bird','rabbit'],'selection':'explicit',
                              'targets':['wand toy','bird','rabbit'],'user_quote':'replace all'})
        self.assertEqual(mapping['status'],'needs_input')
        self.assertIn('wand toy',mapping['question'])

    def test_explicit_mapping_keeps_other_characters_and_invalidates_after_feedback(self):
        self.job['prompt']='替换拿魔杖的角色，其他两个不变'
        mapping=self.resolve({'subjects':['wand toy','bird','rabbit'],'selection':'explicit',
                              'targets':['wand toy'],'user_quote':self.job['prompt']})
        self.assertEqual(mapping['status'],'resolved')
        self.assertEqual(mapping['preserve_subjects'],['bird','rabbit'])
        self.job['subject_mapping']=mapping
        self.assertIsNotNone(subject_mapping.current(self.job))
        self.job['feedback']='改成另一只角色'
        self.assertIsNone(subject_mapping.current(self.job))

    def test_single_reference_subject_does_not_require_question(self):
        mapping=self.resolve({'subjects':['only toy'],'selection':'unique','targets':['only toy'],'user_quote':''})
        self.assertEqual(mapping['status'],'resolved')

    def test_generation_gate_rejects_unresolved_mapping_before_provider_work(self):
        with self.assertRaisesRegex(ValueError,'先明确主体替换'):
            production.prepare_manifest(self.job,self.root,{'unit_id':'u1'})
