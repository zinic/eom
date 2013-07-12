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

    def test_missing_project_id(self):
        env = self.create_env('/v1')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '400 Bad Request')

    def test_simple(self):
        env = self.create_env('/v1', project_id='84197')
        self.governor(env, self.start_response)
        self.assertEquals(self.status, '204 No Content')

    def test_limit(self):
        env = self.create_env('/v1/queues/fizbit/messages', project_id='84197')

        now = time.time()
        limit = 500
        period_sec = 10

        # Go over the limit
        for i in range(limit * 2):
            self.governor(env, self.start_response)
            self.assertEquals(self.status, '204 No Content')

        # Wait until the next time quantum
        normalized = now % (period_sec * 2)
        if normalized < period_sec:
            time.sleep(period_sec - normalized)
        else:
            time.sleep(period_sec * 2 - normalized)

        # This time we should get throttled since in
        # the previous quantum we exceeded the limit
        start = time.time()

        for i in range(limit * 2):
            self.governor(env, self.start_response)
            self.assertEquals(self.status, '204 No Content')

        end = time.time()

        elapsed = end - start
        self.assertAlmostEqual(elapsed, period_sec * 2, delta=2)

    def test_limit_multiprocess(self):

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

        limit = 500
        period_sec = 10
        num_periods = 5
        sec_per_req = float(period_sec) / limit
        url = 'http://127.0.0.1:8783/v1/queues/408284/messages'

        stop = time.time() + period_sec * num_periods

        num_requests = 0

        # Suppress logging
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        while time.time() < stop:
            resp = requests.get(url, headers={'X-Project-ID': 1234})
            self.assertEquals(resp.status_code, 204)

            num_requests += 1

            # Attempt 2x the rate limit
            time.sleep(sec_per_req / 2)

        self.assertAlmostEqual(num_requests, limit * num_periods, delta=300)

        process.terminate()
