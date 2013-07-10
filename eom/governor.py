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

import logging
import re
import time

from oslo.config import cfg
import simplejson as json

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

OPT_GROUP_NAME = 'eom:governor'
OPTIONS = [
    cfg.StrOpt('rates_file'),
    cfg.IntOpt('node_count'),
    cfg.IntOpt('period_sec'),
    cfg.IntOpt('sleep_threshold', default=1),
]

CONF.register_opts(OPTIONS, group=OPT_GROUP_NAME)


class Rate(object):
    """Represents an individual rate configuration."""

    # NOTE(kgriffs): Hard-code slots to make attribute
    # access faster.
    __slots__ = ('name', 'route', 'methods', 'limit', 'target')

    def __init__(self, document, period_sec, node_count):
        """Initializes attributes.

        :param dict document:
        """
        self.name = document['name']
        if 'route' in document:
            self.route = re.compile(document['route'] + '$')
        else:
            self.route = None

        if 'methods' in document:
            self.methods = set(document['methods'])
        else:
            self.methods = None

        self.limit = document['limit']
        self.target = float(self.limit) / period_sec / node_count

    def applies_to(self, method, path):
        """Determines whether this rate applies to a given request.

        :param str method: HTTP method, such as GET or POST
        :param str path: URL path, such as "/v1/queues"
        """
        if self.route is not None and not self.route.match(path):
            return False

        if self.methods is not None and method not in self.methods:
            return False

        return True


def _load_rates(path, period_sec, node_count):
    full_path = CONF.find_file(path)
    if not full_path:
        raise cfg.ConfigFilesNotFoundError([path or '<Empty>'])

    with open(full_path) as fd:
        document = json.load(fd)

    return [Rate(rate_doc, period_sec, node_count) for rate_doc in document]


def _create_calc_sleep(period_sec, counters, sleep_threshold):
    """Creates a closure with the given params for convenience and perf."""

    def calc_sleep(project_id, rate):
        # Alternate between two buckets of
        # counters using a time function.
        now = int(time.time())
        normalized = now % (period_sec * 2)

        if normalized < period_sec:
            current_bucket = 'a'
            previous_bucket = 'b'
        else:
            current_bucket = 'b'
            previous_bucket = 'a'

        # Update counter
        counter_key = project_id + ':' + current_bucket
        try:
            current_count = counters[counter_key]
            counters[counter_key] = current_count + 1
        except KeyError:
            current_count = 1
            counters[counter_key] = 1

        # See if we need to rate-limit based on previous_bucket
        counter_key = project_id + ':' + previous_bucket
        try:
            previous_counter = counters[counter_key]
        except KeyError:
            previous_counter = 0

        if previous_counter > rate.limit:
            # If they had been doing requests at
            # rate.limit then how long would it have
            # taken for them to submit the same
            # number of requests?
            normal_sec = float(previous_counter) / rate.target

            # Slow them down so they can only do
            # rate.limit during period_sec
            seconds_over = period_sec - normal_sec
            sleep_per_request = seconds_over / previous_counter

            # Now, the per-request pause may be too small to sleep
            # on, so we chunk it up over multiple requests
            if sleep_per_request < sleep_threshold:
                batch_size = int(sleep_threshold / sleep_per_request)
                sleep_per_request = sleep_threshold
            else:
                batch_size = 1

            if current_count % batch_size == 0:
                return sleep_per_request

        return 0

    return calc_sleep


def _http_429(start_response):
    """Responds with HTTP 429."""
    start_response('429 Too Many Requests', [('Content-Length', '0')])

    # TODO(kgriffs): Return a helpful message in JSON or XML, depending
    # on the accept header.
    return []


def _http_400(start_response):
    """Responds with HTTP 400."""
    start_response('400 Bad Request', [('Content-Length', '0')])

    # TODO(kgriffs): Return a helpful message in JSON or XML, depending
    # on the accept header.
    return []


# NOTE(kgriffs): Using a functional style since it is more
# performant than an object-oriented one (middleware should
# introduce as little overhead as possible.)
def wrap(app):
    """Wrap a WSGI app with ACL middleware.

    Takes configuration from oslo.config.cfg.CONF.

    :param app: WSGI app to wrap
    :returns: a new WSGI app that wraps the original
    """
    group = CONF[OPT_GROUP_NAME]

    node_count = group['node_count']
    period_sec = group['period_sec']
    sleep_threshold = group['sleep_threshold']

    rates_path = group['rates_file']
    rates = _load_rates(rates_path, period_sec, node_count)

    counters = {}
    calc_sleep = _create_calc_sleep(period_sec, counters, sleep_threshold)

    # WSGI callable
    def middleware(env, start_response):
        path = env['PATH_INFO']
        method = env['REQUEST_METHOD']

        for rate in rates:
            if rate.applies_to(method, path):
                break
        else:
            LOG.debug(_('Requested path not recognized. Full steam ahead!'))
            return app(env, start_response)

        try:
            project_id = env['HTTP_X_PROJECT_ID']
        except KeyError:
            LOG.error(_('Request headers did not include X-Project-ID'))
            return _http_400(start_response)

        delay = calc_sleep(project_id, rate)

        if delay != 0:
            # Stay calm...
            time.sleep(delay)

        # ...and carry on.
        return app(env, start_response)

    return middleware
