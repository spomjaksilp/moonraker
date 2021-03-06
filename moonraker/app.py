# Klipper Web Server Rest API
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license

import os
import mimetypes
import logging
import tornado
from inspect import isclass
from tornado.escape import url_unescape
from tornado.routing import Rule, PathMatches, AnyMatches
from utils import ServerError
from websockets import WebsocketManager, WebSocket
from authorization import AuthorizedRequestHandler, AuthorizedFileHandler
from authorization import Authorization

# These endpoints are reserved for klippy/server communication only and are
# not exposed via http or the websocket
RESERVED_ENDPOINTS = [
    "list_endpoints", "gcode/subscribe_output",
    "register_remote_method"
]


# Status objects require special parsing
def _status_parser(request):
    query_args = request.query_arguments
    args = {}
    for key, vals in query_args.items():
        parsed = []
        for v in vals:
            if v:
                parsed += v.decode().split(',')
        if parsed == []:
            parsed = None
        args[key] = parsed
    return {'objects': args}

# Built-in Query String Parser
def _default_parser(request):
    query_args = request.query_arguments
    args = {}
    for key, vals in query_args.items():
        if len(vals) != 1:
            raise tornado.web.HTTPError(404, "Invalid Query String")
        args[key] = vals[0].decode()
    return args

class MutableRouter(tornado.web.ReversibleRuleRouter):
    def __init__(self, application):
        self.application = application
        self.pattern_to_rule = {}
        super(MutableRouter, self).__init__(None)

    def get_target_delegate(self, target, request, **target_params):
        if isclass(target) and issubclass(target, tornado.web.RequestHandler):
            return self.application.get_handler_delegate(
                request, target, **target_params)

        return super(MutableRouter, self).get_target_delegate(
            target, request, **target_params)

    def has_rule(self, pattern):
        return pattern in self.pattern_to_rule

    def add_handler(self, pattern, target, target_params):
        if pattern in self.pattern_to_rule:
            self.remove_handler(pattern)
        new_rule = Rule(PathMatches(pattern), target, target_params)
        self.pattern_to_rule[pattern] = new_rule
        self.rules.append(new_rule)

    def remove_handler(self, pattern):
        rule = self.pattern_to_rule.pop(pattern, None)
        if rule is not None:
            try:
                self.rules.remove(rule)
            except Exception:
                logging.exception(f"Unable to remove rule: {pattern}")

class APIDefinition:
    def __init__(self, endpoint, http_uri, ws_methods,
                 request_methods, parser):
        self.endpoint = endpoint
        self.uri = http_uri
        self.ws_methods = ws_methods
        if not isinstance(request_methods, list):
            request_methods = [request_methods]
        self.request_methods = request_methods
        self.parser = parser

class MoonrakerApp:
    def __init__(self, config):
        self.server = config.get_server()
        self.tornado_server = None
        self.api_cache = {}
        self.registered_base_handlers = []
        self.max_upload_size = config.getint('max_upload_size', 200)
        self.max_upload_size *= 1024 * 1024

        # Set Up Websocket and Authorization Managers
        self.wsm = WebsocketManager(self.server)
        self.auth = Authorization(config['authorization'])

        mimetypes.add_type('text/plain', '.log')
        mimetypes.add_type('text/plain', '.gcode')
        mimetypes.add_type('text/plain', '.cfg')
        debug = config.getboolean('enable_debug_logging', True)
        enable_cors = config.getboolean('enable_cors', False)

        # Set up HTTP only requests
        self.mutable_router = MutableRouter(self)
        app_handlers = [
            (AnyMatches(), self.mutable_router),
            (r"/websocket", WebSocket,
             {'wsm': self.wsm, 'auth': self.auth}),
            (r"/api/version", EmulateOctoprintHandler,
             {'server': self.server, 'auth': self.auth})]

        self.app = tornado.web.Application(
            app_handlers,
            serve_traceback=debug,
            websocket_ping_interval=10,
            websocket_ping_timeout=30,
            enable_cors=enable_cors)
        self.get_handler_delegate = self.app.get_handler_delegate

        # Register handlers
        logfile = config['cmd_args'].get('logfile')
        self.register_static_file_handler("moonraker.log", logfile)
        self.auth.register_handlers(self)

    def listen(self, host, port):
        self.tornado_server = self.app.listen(
            port, address=host, max_body_size=self.max_upload_size,
            xheaders=True)

    async def close(self):
        if self.tornado_server is not None:
            self.tornado_server.stop()
        await self.wsm.close()
        self.auth.close()

    def register_remote_handler(self, endpoint):
        if endpoint in RESERVED_ENDPOINTS:
            return
        api_def = self._create_api_definition(endpoint)
        if api_def.uri in self.registered_base_handlers:
            # reserved handler or already registered
            return
        logging.info(
            f"Registering remote endpoint - "
            f"HTTP: ({' '.join(api_def.request_methods)}) {api_def.uri}; "
            f"Websocket: {', '.join(api_def.ws_methods)}")
        self.wsm.register_remote_handler(api_def)
        params = {}
        params['server'] = self.server
        params['auth'] = self.auth
        params['arg_parser'] = api_def.parser
        params['remote_callback'] = api_def.endpoint
        self.mutable_router.add_handler(
            api_def.uri, RemoteRequestHandler, params)
        self.registered_base_handlers.append(api_def.uri)

    def register_local_handler(self, uri, request_methods,
                               callback, protocol=["http", "websocket"]):
        if uri in self.registered_base_handlers:
            return
        api_def = self._create_api_definition(
            uri, request_methods, is_remote=False)
        msg = "Registering local endpoint"
        if "http" in protocol:
            msg += f" - HTTP: ({' '.join(request_methods)}) {uri}"
            params = {}
            params['server'] = self.server
            params['auth'] = self.auth
            params['methods'] = request_methods
            params['arg_parser'] = api_def.parser
            params['callback'] = callback
            self.mutable_router.add_handler(uri, LocalRequestHandler, params)
            self.registered_base_handlers.append(uri)
        if "websocket" in protocol:
            msg += f" - Websocket: {', '.join(api_def.ws_methods)}"
            self.wsm.register_local_handler(api_def, callback)
        logging.info(msg)

    def register_static_file_handler(self, pattern, file_path):
        if pattern[0] != "/":
            pattern = "/server/files/" + pattern
        if os.path.isfile(file_path):
            pattern += '()'
        elif os.path.isdir(file_path):
            if pattern[-1] != "/":
                pattern += "/"
            pattern += "(.*)"
        else:
            logging.info(f"Invalid file path: {file_path}")
            return
        logging.debug(f"Registering static file: ({pattern}) {file_path}")
        params = {'server': self.server, 'auth': self.auth, 'path': file_path}
        self.mutable_router.add_handler(pattern, FileRequestHandler, params)

    def register_upload_handler(self, pattern):
        params = {'server': self.server, 'auth': self.auth}
        self.mutable_router.add_handler(pattern, FileUploadHandler, params)

    def remove_handler(self, endpoint):
        api_def = self.api_cache.get(endpoint)
        if api_def is not None:
            self.wsm.remove_handler(api_def.uri)
            self.mutable_router.remove_handler(api_def.ws_method)

    def _create_api_definition(self, endpoint, request_methods=[],
                               is_remote=True):
        if endpoint in self.api_cache:
            return self.api_cache[endpoint]
        if endpoint[0] == '/':
            uri = endpoint
        elif is_remote:
            uri = "/printer/" + endpoint
        else:
            uri = "/server/" + endpoint
        ws_methods = []
        if is_remote:
            # Remote requests accept both GET and POST requests.  These
            # requests execute the same callback, thus they resolve to
            # only a single websocket method.
            ws_methods.append(uri[1:].replace('/', '.'))
            request_methods = ['GET', 'POST']
        else:
            name_parts = uri[1:].split('/')
            if len(request_methods) > 1:
                for req_mthd in request_methods:
                    func_name = req_mthd.lower() + "_" + name_parts[-1]
                    ws_methods.append(".".join(name_parts[:-1] + [func_name]))
            else:
                ws_methods.append(".".join(name_parts))
        if not is_remote and len(request_methods) != len(ws_methods):
            raise self.server.error(
                "Invalid API definition.  Number of websocket methods must "
                "match the number of request methods")
        if endpoint.startswith("objects/"):
            parser = _status_parser
        else:
            parser = _default_parser

        api_def = APIDefinition(endpoint, uri, ws_methods,
                                request_methods, parser)
        self.api_cache[endpoint] = api_def
        return api_def

# ***** Dynamic Handlers*****
class RemoteRequestHandler(AuthorizedRequestHandler):
    def initialize(self, remote_callback, server, auth, arg_parser):
        super(RemoteRequestHandler, self).initialize(server, auth)
        self.remote_callback = remote_callback
        self.query_parser = arg_parser

    async def get(self):
        await self._process_http_request()

    async def post(self):
        await self._process_http_request()

    async def _process_http_request(self):
        args = {}
        if self.request.query:
            args = self.query_parser(self.request)
        try:
            result = await self.server.make_request(
                self.remote_callback, args)
        except ServerError as e:
            raise tornado.web.HTTPError(
                e.status_code, str(e)) from e
        self.finish({'result': result})

class LocalRequestHandler(AuthorizedRequestHandler):
    def initialize(self, callback, server, auth,
                   methods, arg_parser):
        super(LocalRequestHandler, self).initialize(server, auth)
        self.callback = callback
        self.methods = methods
        self.query_parser = arg_parser

    async def get(self):
        if 'GET' in self.methods:
            await self._process_http_request('GET')
        else:
            raise tornado.web.HTTPError(405)

    async def post(self):
        if 'POST' in self.methods:
            await self._process_http_request('POST')
        else:
            raise tornado.web.HTTPError(405)

    async def delete(self):
        if 'DELETE' in self.methods:
            await self._process_http_request('DELETE')
        else:
            raise tornado.web.HTTPError(405)

    async def _process_http_request(self, method):
        args = {}
        if self.request.query:
            args = self.query_parser(self.request)
        try:
            result = await self.callback(self.request.path, method, args)
        except ServerError as e:
            raise tornado.web.HTTPError(
                e.status_code, str(e)) from e
        self.finish({'result': result})


class FileRequestHandler(AuthorizedFileHandler):
    def set_extra_headers(self, path):
        # The call below shold never return an empty string,
        # as the path should have already been validated to be
        # a file
        basename = os.path.basename(self.absolute_path)
        self.set_header(
            "Content-Disposition", f"attachment; filename={basename}")

    async def delete(self, path):
        path = self.request.path.lstrip("/").split("/", 2)[-1]
        path = url_unescape(path)
        file_manager = self.server.lookup_plugin('file_manager')
        try:
            filename = await file_manager.delete_file(path)
        except self.server.error as e:
            if e.status_code == 403:
                raise tornado.web.HTTPError(
                    403, "File is loaded, DELETE not permitted")
            else:
                raise tornado.web.HTTPError(e.status_code, str(e))
        self.finish({'result': filename})

class FileUploadHandler(AuthorizedRequestHandler):
    def initialize(self, server, auth):
        super(FileUploadHandler, self).initialize(server, auth)

    async def post(self):
        file_manager = self.server.lookup_plugin('file_manager')
        try:
            result = await file_manager.process_file_upload(self.request)
        except ServerError as e:
            raise tornado.web.HTTPError(
                e.status_code, str(e))
        self.finish(result)


class EmulateOctoprintHandler(AuthorizedRequestHandler):
    def get(self):
        self.finish({
            'server': "1.1.1",
            'api': "0.1",
            'text': "OctoPrint Upload Emulator"})
