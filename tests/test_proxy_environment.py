import unittest
from unittest.mock import patch

from app import config


class ProxyEnvironmentTests(unittest.TestCase):
    def configure(self, env, proxies, supported=True):
        with patch.object(config, 'getproxies', return_value=proxies), \
             patch.object(config, 'runtime_settings', return_value={}), \
             patch.object(config, '_node_supports_proxy', return_value=supported):
            config._proxy_environment(env)
        return env

    def test_system_proxy_reaches_node_and_local_hosts_bypass_it(self):
        env = self.configure({'NODE_OPTIONS': '--max-old-space-size=2048'},
                             {'https': 'http://127.0.0.1:6789', 'no': '*.internal'})
        self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:6789')
        self.assertIn('--max-old-space-size=2048', env['NODE_OPTIONS'])
        self.assertIn('--use-env-proxy', env['NODE_OPTIONS'])
        for host in ('*.internal', 'localhost', '127.0.0.1', '::1'):
            self.assertIn(host, env['NO_PROXY'].split(','))
        self.assertEqual(env['NO_PROXY'], env['no_proxy'])

    def test_explicit_proxy_and_bypass_are_preserved(self):
        env = {'https_proxy': 'http://explicit:8888', 'no_proxy': 'example.com',
               'NODE_OPTIONS': '--use-env-proxy'}
        self.configure(env, {'https': 'http://system:6789'})
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertEqual(env['https_proxy'], 'http://explicit:8888')
        self.assertIn('example.com', env['NO_PROXY'])
        self.assertEqual(env['NODE_OPTIONS'], '--use-env-proxy')

    def test_no_proxy_does_not_enable_node_flag(self):
        self.assertEqual(self.configure({}, {}), {})

    def test_old_node_does_not_receive_unsupported_flag(self):
        env = self.configure({}, {'https': 'http://localhost:6789'}, supported=False)
        self.assertNotIn('NODE_OPTIONS', env)

    def test_explicit_host_bypass_keeps_proxy_for_other_endpoints(self):
        env={}
        with patch.object(config,'getproxies',return_value={'https':'http://127.0.0.1:6789'}), \
             patch.object(config,'runtime_settings',return_value={'proxy_bypass_hosts':['www.autodl.art']}), \
             patch.object(config,'_node_supports_proxy',return_value=True):
            config._proxy_environment(env)
        self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:6789')
        self.assertIn('www.autodl.art',env['NO_PROXY'].split(','))
        self.assertNotIn('ark.cn-beijing.volces.com',env['NO_PROXY'].split(','))

    def test_python_requests_use_the_same_exact_host_rule(self):
        with patch.object(config,'proxy_bypass_hosts',return_value=['ark.cn-beijing.volces.com']):
            with config.http_session('https://ark.cn-beijing.volces.com/api/v3') as direct:
                self.assertFalse(direct.trust_env)
            with config.http_session('https://other.example.com') as routed:
                self.assertTrue(routed.trust_env)


if __name__ == '__main__':
    unittest.main()
