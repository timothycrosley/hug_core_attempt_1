"""hug/api.py

Defines the dynamically generated Hug API object that is responsible for storing all routes and state within a module

Copyright (C) 2016  Timothy Edmund Crosley

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED
TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

"""
from __future__ import absolute_import

import sys
from collections import OrderedDict, namedtuple
from distutils.util import strtobool
from functools import partial
from itertools import chain
from types import ModuleType
from wsgiref.simple_server import make_server

import falcon
import hug.defaults
import hug.output_format
from falcon import HTTP_METHODS
from hug import introspect
from hug._async import asyncio, ensure_future
from hug._version import current


class InterfaceAPI(object):
    """Defines the per-interface API which defines all shared information for a specific interface, and how it should
        be exposed
    """
    __slots__ = ('api', )

    def __init__(self, api):
        self.api = api


class ModuleSingleton(type):
    """Defines the module level __hug__ singleton"""

    def __call__(cls, module=None, *args, **kwargs):
        if isinstance(module, API):
            return module

        if type(module) == str:
            if module not in sys.modules:
                sys.modules[module] = ModuleType(module)
            module = sys.modules[module]
        elif module is None:
            return super().__call__(*args, **kwargs)

        if not '__hug__' in module.__dict__:
            def api_auto_instantiate(*args, **kwargs):
                if not hasattr(module, '__hug_serving__'):
                    module.__hug_wsgi__ = module.__hug__.http.server()
                    module.__hug_serving__ = True
                return module.__hug_wsgi__(*args, **kwargs)

            module.__hug__ = super().__call__(module, *args, **kwargs)
            module.__hug_wsgi__ = api_auto_instantiate
        return module.__hug__


class API(object, metaclass=ModuleSingleton):
    """Stores the information necessary to expose API calls within this module externally"""
    __slots__ = ('module', '_directives', '_http', '_cli', '_context', '_context_factory', '_delete_context',
                 '_startup_handlers', 'started', 'name', 'doc', 'cli_error_exit_codes')

    def __init__(self, module=None, name='', doc='', cli_error_exit_codes=False):
        self.module = module
        if module:
            self.name = name or module.__name__ or ''
            self.doc = doc or module.__doc__ or ''
        else:
            self.name = name
            self.doc = doc
        self.started = False
        self.cli_error_exit_codes = cli_error_exit_codes

    def directives(self):
        """Returns all directives applicable to this Hug API"""
        directive_sources = chain(hug.defaults.directives.items(), getattr(self, '_directives', {}).items())
        return {'hug_' + directive_name: directive for directive_name, directive in directive_sources}

    def directive(self, name, default=None):
        """Returns the loaded directive with the specified name, or default if passed name is not present"""
        return getattr(self, '_directives', {}).get(name,  hug.defaults.directives.get(name, default))

    def add_directive(self, directive):
        self._directives = getattr(self, '_directives', {})
        self._directives[directive.__name__] = directive

    def handlers(self):
        """Returns all registered handlers attached to this API"""
        if getattr(self, '_http'):
            yield from self.http.handlers()
        if getattr(self, '_cli'):
            yield from self.cli.handlers()

    @property
    def http(self):
        if not hasattr(self, '_http'):
            self._http = HTTPInterfaceAPI(self)
        return self._http

    @property
    def cli(self):
        if not hasattr(self, '_cli'):
            self._cli = CLIInterfaceAPI(self, error_exit_codes=self.cli_error_exit_codes)
        return self._cli

    @property
    def context_factory(self):
        return getattr(self, '_context_factory', hug.defaults.context_factory)

    @context_factory.setter
    def context_factory(self, context_factory_):
        self._context_factory = context_factory_

    @property
    def delete_context(self):
        return getattr(self, '_delete_context', hug.defaults.delete_context)

    @delete_context.setter
    def delete_context(self, delete_context_):
        self._delete_context = delete_context_

    @property
    def context(self):
        if not hasattr(self, '_context'):
            self._context = {}
        return self._context

    def extend(self, api, route="", base_url=""):
        """Adds handlers from a different Hug API to this one - to create a single API"""
        api = API(api)

        if hasattr(api, '_http'):
            self.http.extend(api.http, route, base_url)

        for directive in getattr(api, '_directives', {}).values():
            self.add_directive(directive)

        for startup_handler in (api.startup_handlers or ()):
            self.add_startup_handler(startup_handler)

    def add_startup_handler(self, handler):
        """Adds a startup handler to the hug api"""
        if not self.startup_handlers:
            self._startup_handlers = []

        self.startup_handlers.append(handler)

    def _ensure_started(self):
        """Marks the API as started and runs all startup handlers"""
        if not self.started:
            async_handlers = [startup_handler for startup_handler in self.startup_handlers if
                              introspect.is_coroutine(startup_handler)]
            if async_handlers:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(asyncio.gather(*[handler(self) for handler in async_handlers], loop=loop))
            for startup_handler in self.startup_handlers:
                if not startup_handler in async_handlers:
                    startup_handler(self)

    @property
    def startup_handlers(self):
        return getattr(self, '_startup_handlers', ())


def from_object(obj):
    """Returns a Hug API instance from a given object (function, class, instance)"""
    return API(obj.__module__)
