# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR ONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import testtools

from oslo.config import cfg

import eom.rbac

CONF = cfg.CONF


def conf_path(filename):
    """Returns the full path to the specified Marconi conf file.

    :param filename: Name of the conf file to find (e.g.,
                     'wsgi_memory.conf')
    """

    module_dir = os.path.abspath(os.path.dirname(__file__))
    parent = os.path.dirname(module_dir)
    return os.path.join(parent, 'etc', filename)


def _create_env(path, roles=None, method='GET'):
    if not isinstance(roles, str):
        roles = ','.join(roles or [])

    return {
        'HTTP_X_ROLES': roles,
        'PATH_INFO': path,
        'REQUEST_METHOD': method
    }


def app(env, start_response):
    start_response('204 No Content', [])


class TestRBAC(testtools.TestCase):

    def setUp(self):
        super(TestRBAC, self).setUp()

        config_file = conf_path('eom.conf-sample')
        CONF(args=[], default_config_files=[config_file])

        self.rbac = eom.rbac.wrap(app)

    def _start_response(self, status, headers):
        self.status = status
        self.headers = headers

    def test_noacl(self):
        env = _create_env('/v1')
        self.rbac(env, self._start_response)
        self.assertEquals(self.status, '204 No Content')

    def test_noroles(self):
        env = _create_env('/v1/queues')
        self.rbac(env, self._start_response)
        self.assertEquals(self.status, '403 Forbidden')

    def test_unrecognized_role(self):
        env = _create_env('/v1/queues', 'super:fly')
        self.rbac(env, self._start_response)
        self.assertEquals(self.status, '403 Forbidden')

    def test_can_read(self):
        for http_method in ('GET', 'HEAD', 'OPTIONS'):
            env = _create_env('/v1/queues', 'queuing:observer',
                              method=http_method)
            self.rbac(env, self._start_response)
            self.assertEquals(self.status, '204 No Content')

    def test_no_read(self):
        for http_method in ('GET', 'HEAD', 'OPTIONS'):
            env = _create_env('/v1/queues', 'queuing:producer',
                              method=http_method)
            self.rbac(env, self._start_response)
            self.assertEquals(self.status, '403 Forbidden')

    def test_can_write(self):
        for http_method in ('PATCH', 'POST', 'PUT'):
            env = _create_env('/v1/queues', 'queuing:admin',
                              method=http_method)
            self.rbac(env, self._start_response)
            self.assertEquals(self.status, '204 No Content')

    def test_no_write(self):
        for http_method in ('PATCH', 'POST', 'PUT'):
            env = _create_env('/v1/queues', 'observer',
                              method=http_method)
            self.rbac(env, self._start_response)
            self.assertEquals(self.status, '403 Forbidden')

    def test_can_delete(self):
        env = _create_env('/v1/queues', 'queuing:gc',
                          method='DELETE')
        self.rbac(env, self._start_response)
        self.assertEquals(self.status, '204 No Content')

    def test_no_delete(self):
        env = _create_env('/v1/queues', 'queuing:producer',
                          method='DELETE')
        self.rbac(env, self._start_response)
        self.assertEquals(self.status, '403 Forbidden')
