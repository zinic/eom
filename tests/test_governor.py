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

import io
import logging
import multiprocessing
import sys
import time
from wsgiref import simple_server

import eom.governor
import requests

from tests import util


class TestGovernor(util.TestCase):

    def setUp(self):
        super(TestGovernor, self).setUp()

        self.governor = eom.governor.wrap(util.app)

        config = eom.governor.CONF['eom:governor']
        self.node_count = config['node_count']
        self.period_sec = config['period_sec']
        rates = eom.governor._load_rates(config['rates_file'],
                                         self.period_sec, self.node_count)

        self.test_rate = rates[0]
        self.soft_limit = self.test_rate.soft_limit
        self.hard_limit = self.test_rate.hard_limit
        self.test_url = '/v1/queues/fizbit/messages'

        self.default_rate = rates[1]

    def _quantum_leap(self):
        # Wait until the next time quantum
        normalized = time.time() % (self.period_sec * 2)
        if normalized < self.period_sec:
            time.sleep(self.period_sec - normalized)
        else:
            time.sleep(self.period_sec * 2 - normalized)

    def test_missing_project_id(self):
        env = self.create_env('/v1')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '400 Bad Request')

    def test_simple(self):
        env = self.create_env('/v1', project_id='84197')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '204 No Content')

    def test_soft_limit(self):
        elapsed = self._test_soft_limit(self.soft_limit, 'GET')
        self.assertAlmostEqual(elapsed, self.period_sec * 2, delta=2)

    def test_soft_limit_default(self):
        elapsed = self._test_soft_limit(self.default_rate.soft_limit, 'PUT')
        self.assertAlmostEqual(elapsed, self.period_sec * 2, delta=2)

    def test_soft_limit_multiprocess(self):
        self._test_limit_multiprocess(self.soft_limit, 204)

    def test_hard_limit_multiprocess(self):
        self._test_limit_multiprocess(self.hard_limit, 429)

    #----------------------------------------------------------------------
    # Helpers
    #----------------------------------------------------------------------

    def _test_soft_limit(self, soft_limit, http_method):
        env = self.create_env(self.test_url, project_id='84197',
                              method=http_method)

        # Go over the limit
        num_requests = soft_limit * 2
        for i in range(num_requests):
            self.governor(env, self.start_response)
            self.assertEquals(self.status, '204 No Content')

        # This time we should get throttled since in
        # the previous quantum we exceeded the limit
        self._quantum_leap()

        start = time.time()

        for i in range(num_requests):
            self.governor(env, self.start_response)
            self.assertEquals(self.status, '204 No Content')

        end = time.time()

        elapsed = end - start

        return elapsed

    def _test_limit_multiprocess(self, limit, expected_status):

        def run_server():
            sys.stderr = io.BytesIO()  # Suppress logging

            httpd = simple_server.make_server('127.0.0.1', 8783,
                                              self.governor)
            httpd.serve_forever()

        process = multiprocessing.Process(target=run_server)
        process.daemon = True
        process.start()

        # Give the process a moment to start up
        time.sleep(0.1)

        num_periods = 6
        sec_per_req = float(self.period_sec) / limit
        url = 'http://127.0.0.1:8783' + self.test_url

        num_requests = 0

        # Suppress logging
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        for i in range(limit + 5):
            requests.get(url, headers={'X-Project-ID': 1234})

        # Start out at the beginning of a time bucket
        self._quantum_leap()

        start = time.time()
        stop = start + self.period_sec * num_periods
        while time.time() < stop:
            resp = requests.get(url, headers={'X-Project-ID': 1234})
            self.assertEquals(resp.status_code, expected_status)

            num_requests += 1

            # Attempt 2x the rate limit
            time.sleep(sec_per_req / 2)

        if expected_status == 204:
            # We would have slept so we can predict
            # the rate.
            self.assertAlmostEqual(num_requests, limit * num_periods,
                                   delta=(200 / self.node_count))

        process.terminate()
