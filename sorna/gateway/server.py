'''
The main web / websocket server
'''

import asyncio
from ipaddress import ip_address
import logging
import signal
import ssl
import sys

import aiohttp
from aiohttp import web
import aiotools
import asyncpgsa
import uvloop
try:
    import datadog
    datadog_available = True
except ImportError:
    datadog_available = False

from sorna.common.argparse import ipaddr, path, port_no, host_port_pair, HostPortPair
from sorna.common.utils import env_info
from ..manager import __version__
from . import GatewayStatus
from .exceptions import (SornaError, GenericNotFound,
                         GenericBadRequest, InternalServerError)
from .auth import init as auth_init, shutdown as auth_shutdown
from .config import load_config, init_logger
from .etcd import init as etcd_init, shutdown as etcd_shutdown
from .events import init as event_init, shutdown as event_shutdown
from .kernel import init as kernel_init, shutdown as kernel_shutdown
from .ratelimit import init as rlim_init, shutdown as rlim_shutdown
from .utils import prettify_traceback

VALID_VERSIONS = frozenset([
    'v1.20160915',
    'v2.20170315',
    'v3.20170615',
])
LATEST_API_VERSION = 'v3.20170615'

log = logging.getLogger('sorna.gateway.server')


async def hello(request) -> web.Response:
    '''
    Returns the API version number.
    '''
    return web.json_response({'version': LATEST_API_VERSION})


async def on_prepare(request, response):
    response.headers['Server'] = 'Sorna-API/' + LATEST_API_VERSION


async def api_middleware_factory(app, handler):
    async def api_middleware_handler(request):
        method_override = request.headers.get('X-Method-Override', None)
        if method_override:
            request = request.clone(method=method_override)
        if request.rel_url.path.startswith('/v1'):
            path_ver = 1
        elif request.rel_url.path.startswith('/v2'):
            path_ver = 2
        else:
            raise GenericBadRequest('Unsupported API version.')
        hdr_ver = request.headers.get('X-Sorna-Version', None)
        if hdr_ver is None:
            raise GenericBadRequest('API version missing in headers.')
        if hdr_ver not in VALID_VERSIONS:
            raise GenericBadRequest('Invalid API version.')
        if not hdr_ver.startswith(f'v{path_ver}.'):
            raise GenericBadRequest('Path and header API version mismatch.')
        request['api_version'] = path_ver
        resp = (await handler(request))
        return resp
    return api_middleware_handler


async def exception_middleware_factory(app, handler):
    async def exception_middleware_handler(request):
        try:
            if app['datadog']:
                app['datadog'].statsd.increment('sorna.gateway.api.requests')
            resp = (await handler(request))
        except SornaError as ex:
            if app['datadog']:
                statsd = app['datadog'].statsd
                statsd.increment('sorna.gateway.api.failures')
                statsd.increment(f'sorna.gateway.api.status.{ex.status_code}')
            raise
        except web.HTTPException as ex:
            if app['datadog']:
                statsd = app['datadog'].statsd
                statsd.increment('sorna.gateway.api.failures')
                statsd.increment(f'sorna.gateway.api.status.{ex.status_code}')
            if ex.status_code == 404:
                raise GenericNotFound
            log.warning(f'Bad request: {ex!r}')
            raise GenericBadRequest
        except Exception as ex:
            log.exception('Uncaught exception in HTTP request handlers')
            title = f'Exception from {request.method} {request.rel_url.path}'
            tag = f'path:{request.rel_url.path}'
            text = prettify_traceback(ex)
            if app['datadog']:
                app['datadog'].statsd.event(
                    title, text,
                    tags=['sorna', 'exception'],
                    aggregation_key=request.rel_url.path,
                    alert_type='error')
            raise InternalServerError
        else:
            if app['datadog']:
                app['datadog'].statsd.increment(f'sorna.gateway.api.status.{resp.status}')
            return resp
    return exception_middleware_handler


async def gw_init(app):
    app.on_response_prepare.append(on_prepare)
    app.router.add_route('GET', '/v1', hello)
    app.router.add_route('GET', '/v2', hello)
    app['status'] = GatewayStatus.STARTING
    app['datadog'] = None
    if datadog_available:
        if app.config.datadog_api_key is None:
            log.warning('datadog logging disabled (missing API key)')
        else:
            datadog.initialize(
                api_key=app.config.datadog_api_key,
                app_key=app.config.datadog_app_key)
            app['datadog'] = datadog
            log.info('datadog logging enabled')
    else:
        log.warning('datadog logging disabled (no datadog package)')

    app.dbpool = await asyncpgsa.create_pool(
        host=str(app.config.db_addr[0]),
        port=app.config.db_addr[1],
        database=app.config.db_name,
        user=app.config.db_user,
        password=app.config.db_password,
        min_size=4, max_size=16,
    )
    app.middlewares.append(exception_middleware_factory)
    app.middlewares.append(api_middleware_factory)


async def gw_shutdown(app):
    await app.dbpool.close()


@aiotools.actxmgr
async def server_main(loop, pidx, _args):

    app = web.Application()
    app.config = _args[0]
    app.sslctx = None
    if app.config.ssl_cert and app.config.ssl_key:
        app.sslctx = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        app.sslctx.load_cert_chain(str(app.config.ssl_cert),
                                   str(app.config.ssl_key))
    if app.config.service_port == 0:
        app.config.service_port = 8443 if app.sslctx else 8080

    await etcd_init(app)
    await event_init(app)
    await gw_init(app)
    await auth_init(app)
    await rlim_init(app)
    await kernel_init(app)

    web_handler = app.make_handler()
    server = await loop.create_server(
        web_handler,
        host=str(app.config.service_ip),
        port=app.config.service_port,
        reuse_port=True,
        ssl=app.sslctx,
    )
    log.info('started.')

    yield

    log.info('shutting down...')
    server.close()
    await server.wait_closed()

    await kernel_shutdown(app)
    await rlim_shutdown(app)
    await auth_shutdown(app)
    await gw_shutdown(app)
    await event_shutdown(app)
    await etcd_shutdown(app)

    await app.shutdown()
    await web_handler.finish_connections(60.0)
    await app.cleanup()


def gw_args(parser):
    parser.add('--namespace', env_var='SORNA_NAMESPACE', type=str, default='local',
               help='The namespace of this Sorna cluster. (default: local)')
    parser.add('--etcd-addr', env_var='SORNA_ETCD_ADDR', type=host_port_pair,
               default=HostPortPair(ip_address('127.0.0.1'), 2379),
               help='The host:port pair of the etcd cluster or its proxy.')
    parser.add('--mq-addr', env_var='SORNA_MQ_ADDR', type=host_port_pair,
               default=HostPortPair(ip_address('127.0.0.1'), 5672),
               help='The host:port pair of the RabbitMQ or its proxy.')
    parser.add('--mq-user', env_var='SORNA_MQ_USER', type=str, default='sorna',
               help='The username to log into the AMQP service. (default: sorna)')
    parser.add('--mq-password', env_var='SORNA_MQ_PASSWORD', type=str, default='develove',
               help='The password to log into the AMQP service. (default: develove)')

    parser.add('--service-ip', env_var='SORNA_SERVICE_IP', type=ipaddr, default=ip_address('0.0.0.0'),
               help='The IP where the API gateway server listens on. (default: 0.0.0.0)')
    parser.add('--service-port', env_var='SORNA_SERVICE_PORT', type=port_no, default=0,
               help='The TCP port number where the API gateway server listens on. '
                    '(default: 8080, 8443 when SSL is enabled) '
                    'To run in production, you need the root privilege to use the standard 80/443 ports.')
    parser.add('--ssl-cert', env_var='SORNA_SSL_CERT', type=path, default=None,
               help='The path to an SSL certificate file. '
                    'It may contain inter/root CA certificates as well. '
                    '(default: None)')
    parser.add('--ssl-key', env_var='SORNA_SSL_KEY', type=path, default=None,
               help='The path to the private key used to make requests for the SSL certificate. '
                    '(default: None)')
    parser.add('--gpu-instances', env_var='SORNA_GPU_INSTANCES', type=str, default=None,
               help='Manually set list of GPU-enabled agent instance IDs.')
    if datadog_available:
        parser.add('--datadog-api-key', env_var='DATADOG_API_KEY', type=str, default=None,
                   help='The API key for Datadog monitoring agent.')
        parser.add('--datadog-app-key', env_var='DATADOG_APP_KEY', type=str, default=None,
                   help='The application key for Datadog monitoring agent.')

    # to deprecate
    parser.add('--events-port', env_var='SORNA_EVENTS_PORT', type=port_no, default=5002,
               help='The TCP port number where the event server listens on.')


def main():

    config = load_config(extra_args_func=gw_args)
    init_logger(config)

    log.info(f'Sorna Gateway {__version__}')
    log.info(f'runtime: {env_info()}')

    log_config = logging.getLogger('sorna.gateway.config')
    log_config.debug('debug mode enabled.')

    if config.debug:
        aiohttp.log.server_logger.setLevel('DEBUG')
        aiohttp.log.access_logger.setLevel('DEBUG')
    else:
        aiohttp.log.server_logger.setLevel('WARNING')
        aiohttp.log.access_logger.setLevel('WARNING')

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    aiotools.start_server(server_main, num_proc=1, args=(config,))
    log.info('terminated.')


if __name__ == '__main__':
    main()
