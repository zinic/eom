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

from oslo.config import cfg
import simplejson as json

LOG = logging.getLogger(__name__)
CONF = cfg.CONF

OPT_GROUP_NAME = 'eom'
OPTION_NAME = 'governor_file'

CONF.register_opt(cfg.StrOpt(OPTION_NAME, default=[]),
                  group=OPT_GROUP_NAME)


class Rate(object):
    """Represents an individual rate configuration."""

    __slots__ = ('name', 'route', 'methods', 'limit')

    def __init__(self, document):
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


def _load_rates(path):
    full_path = CONF.find_file(path)
    if not full_path:
        raise cfg.ConfigFilesNotFoundError([path])

    with open(full_path) as fd:
        document = json.load(fd)

    node_count = document['node_count']
    period_sec = document['period_sec']
    rates = [Rate(rate_doc) for rate_doc in document['rates']]

    return node_count, period_sec, rates


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
    rates_path = group[OPTION_NAME]
    node_count, period_sec, rates = _load_rates(rates_path)

    # WSGI callable
    def middleware(env, start_response):
        path = env['PATH_INFO']
        method = env['REQUEST_METHOD']

        for rate in rates:
            if rate.applies_to(method, path):
                break
        else:
            LOG.debug(_('Requested path not recognized. Not limiting.'))
            return app(env, start_response)

        try:
            project_id = env['HTTP_X_PROJECT_ID']
        except KeyError:
            LOG.error(_('Request headers did not include X-Project-ID'))
            return _http_400(start_response)

        # Do the rate limiting here

        # Stay calm and carry on
        return app(env, start_response)

    return middleware
