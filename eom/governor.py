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
    cfg.IntOpt('node_count', default=1),
    cfg.IntOpt('period_sec', default=10),
    cfg.FloatOpt('sleep_threshold', default=0.01),
]

CONF.register_opts(OPTIONS, group=OPT_GROUP_NAME)


class Rate(object):
    """Represents an individual rate configuration."""

    # NOTE(kgriffs): Hard-code slots to make attribute
    # access faster.
    __slots__ = (
        'name',
        'route',
        'methods',
        'soft_limit',
        'hard_limit',
        'target',
    )

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

        self.soft_limit = document['soft_limit'] / node_count
        self.hard_limit = document['hard_limit'] / node_count
        self.target = float(self.soft_limit) / period_sec

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


class HardLimitError(Exception):
    pass


def _load_rates(path, period_sec, node_count):
    full_path = CONF.find_file(path)
    if not full_path:
        raise cfg.ConfigFilesNotFoundError([path or '<Empty>'])

    with open(full_path) as fd:
        document = json.load(fd)

    return [Rate(rate_doc, period_sec, node_count)
            for rate_doc in document]


def _get_counter_key(project_id, bucket):
    return project_id + ':bucket:' + bucket


def _get_throttle_key(project_id):
    return project_id + ':throttle_until'


# TODO(kgriffs): Consider converting to closure-style
class Cache(object):
    __slots__ = ('store',)

    def __init__(self):
        self.store = {}

    def inc_counter(self, project_id, bucket):
        key = _get_counter_key(project_id, bucket)
        try:
            count = self.store[key] + 1
            self.store[key] = count
        except KeyError:
            count = 1
            self.store[key] = 1

        return count

    def get_counter(self, project_id, bucket):
        key = _get_counter_key(project_id, bucket)
        try:
            return self.store[key]
        except KeyError:
            return 0

    def reset_counter(self, project_id, bucket):
        key = _get_counter_key(project_id, bucket)
        self.store[key] = 0

    def set_throttle(self, project_id, period_sec):
        key = _get_throttle_key(project_id)
        self.store[key] = time.time() + period_sec

    def is_throttled(self, project_id):
        key = _get_throttle_key(project_id)
        if key not in self.store:
            return False

        throttle_until = self.store[key]
        now = time.time()

        return now < throttle_until


def _create_calc_sleep(period_sec, cache, sleep_threshold):
    """Creates a closure with the given params for convenience and perf."""

    ctx = {'last_bucket': None}

    def calc_sleep(project_id, rate):
        # Alternate between two buckets of
        # counters using a time function.
        now = time.time()
        normalized = now % (period_sec * 2)

        if normalized < period_sec:
            current_bucket = 'a'
            previous_bucket = 'b'
        else:
            current_bucket = 'b'
            previous_bucket = 'a'

        if ctx['last_bucket'] != current_bucket:
            cache.reset_counter(project_id, current_bucket)
            ctx['last_bucket'] = current_bucket

        current_count = cache.inc_counter(project_id, current_bucket)
        previous_count = cache.get_counter(project_id, previous_bucket)

        if previous_count > rate.hard_limit:
            raise HardLimitError()

        if previous_count > rate.soft_limit:
            # If they had been doing requests at rate.soft_limit then how
            # long would it have taken for them to submit the same
            # number of requests?
            normalized_sec = float(previous_count) / rate.target

            # Slow them down so they can only do rate.soft_limit during
            # period_sec. Do this by delaying each request so that
            # taken together, all requests will take the amount of
            # time they should have taken had they followed the
            # limit during the last time quantum.
            sleep_per_request = normalized_sec / previous_count

            # Allow the rate to slightly exceed the limit so
            # that when we cross over to the next time epoch,
            # we will continue throttling. Otherwise, we can
            # thrash between throttling and not throttling.
            sleep_offset = (0.2 / previous_count)

            # Now, the per-request pause may be too small to sleep
            # on, so we chunk it up over multiple requests. If
            # the sleep time is too small, it will be less
            # accurate as well as introducing too much context-
            # switching overhead that could affect other requests
            # not related to this project ID.
            if sleep_per_request < sleep_threshold:
                batch_size = int(sleep_threshold / sleep_per_request)

                # Only sleep every N requests
                if current_count % batch_size == 0:
                    sleep_sec = sleep_per_request * batch_size
                    return sleep_sec - (sleep_offset * batch_size)

            else:
                # Sleep on every request
                return sleep_per_request - sleep_offset

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

    cache = Cache()
    calc_sleep = _create_calc_sleep(period_sec, cache, sleep_threshold)

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

        try:
            sleep_sec = calc_sleep(project_id, rate)
        except HardLimitError:
            if LOG.getEffectiveLevel() == logging.DEBUG:
                logline = _('Hit hard limit of %(rate)d per sec. for '
                            'project %(project_id)s according to '
                            'rate rule "%(name)s"')
                vars = {
                    'rate': rate.hard_limit / rate.period_sec,
                    'project_id': project_id,
                    'name': rate.name,
                }

                LOG.debug(logline % vars)

            return _http_429(start_response)

        if sleep_sec != 0:
            if LOG.getEffectiveLevel() == logging.DEBUG:
                logline = _('Sleeping %(sleep_sec)f sec. for '
                            'project %(project_id)s to limit '
                            'rate to %(limit)d according to '
                            'rate rule "%(name)s"')
                vars = {
                    'sleep_sec': sleep_sec,
                    'project_id': project_id,
                    'limit': rate.soft_limit,
                    'name': rate.name,
                }

                LOG.debug(logline % vars)

            # Keep calm...
            time.sleep(sleep_sec)

        # ...and carry on.
        return app(env, start_response)

    return middleware
