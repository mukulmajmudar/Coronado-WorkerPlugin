import json
import traceback
from datetime import timedelta
from functools import partial
from uuid import uuid4
import logging
import importlib
import asyncio
import concurrent.futures

import tornado.concurrent
import tornado.web
from tornado import escape
from tornado.ioloop import IOLoop
from tornado.platform.asyncio import AsyncIOMainLoop

from Coronado.Plugin import AppPlugin as AppPluginBase, \
        CommandLinePlugin as CLPluginBase

logger = logging.getLogger(__name__)

config = \
{
    # "producer" or "consumer"
    'workerMode': None,

    'workerShutdownDelay': 5.0
}

class AppPlugin(AppPluginBase):
    context = None

    # pylint: disable=unused-argument
    def start(self, context):
        self.context = context

        # Install asyncio/tornado bridge if not already initialized
        if not IOLoop.initialized():
            AsyncIOMainLoop().install()

        if 'ioloop' not in context:
            context['ioloop'] = IOLoop.current()

        # Start a worker or proxy based on mode
        mode = context['workerMode']
        if mode == 'consumer':
            # Get work handlers
            workHandlers = self.getAppSpecific('getWorkHandlers')

            # Convert to Tornado-style tuple
            workHandlers = [mapping + (self.context,)
                    for mapping in zip(list(workHandlers.keys()),
                        list(workHandlers.values()))]

            # Create a consumer
            self.context['worker'] = self.getConsumerClass()(
                    workHandlers, self.context)

            self.context['ioloop'].run_sync(self.context['worker'].start)

            self.callAppSpecific('start', self, self.context)

        elif mode == 'producer':
            # Create a producer
            self.context['worker'] = self.getProducerClass()(self.context)

            self.context['ioloop'].run_sync(self.context['worker'].start)

        self.context['shortcutAttrs'].append('worker')


    async def destroy(self, context):
        await self.context['worker'].destroy()
        if self.context['workerMode'] == 'consumer':
            self.callAppSpecific('destroy', self, self.context)


    def callAppSpecific(self, functionName, *args, **kwargs):
        versionMod = importlib.import_module(
                self.context['appPackage'].__name__ + '.Worker')
        if hasattr(versionMod, functionName):
            getattr(versionMod, functionName)(*args, **kwargs)


    def getAppSpecific(self, functionName, *args, **kwargs):
        result = {}
        versionMod = importlib.import_module(
                self.context['appPackage'].__name__ + '.Worker')
        if hasattr(versionMod, functionName):
            result = getattr(versionMod, functionName)(*args, **kwargs)

        return result


    def getProducerClass(self):
        raise NotImplementedError()

    def getConsumerClass(self):
        raise NotImplementedError()


class WorkerException(Exception):
    tag = None
    keys = None

    def __init__(self, message='', tag='', **kwargs):
        super().__init__(message)
        self.tag = tag
        self.keys = list(kwargs.keys())
        for key, value in list(kwargs.items()):
            if key == 'tag':
                continue
            setattr(self, key, value)


    def getData(self):
        data = {}
        for key in self.keys:
            data[key] = getattr(self, key)

        return data


class ResponseTimeout(WorkerException):
    pass


class WorkerInterface(object):

    def start(self):
        raise NotImplementedError()

    def destroy(self, *args, **kwargs):
        pass


class Producer(WorkerInterface):

    def __init__(self, ioloop=None):
        self._ioloop = ioloop is not None and ioloop or IOLoop.current()


    # pylint: disable=too-many-arguments
    def request(self, tag, body, contentType='application/json',
            contentEncoding='utf-8', expectResponse=False,
            timeout=timedelta(seconds=60), **kwargs):
        '''
        Send a request to a worker.

        Keyword arguments:
        ------------------
        tag: Work tag that is mapped to the desired work handler.
        body: Request body
        contentType: Content type of the body (default: 'application/json')
        contentEncoding: Encoding of the body (default: 'utf-8')
        expectResponse: Whether to expect a response for this
          request (default: False)
        timeout: Timeout for the request (default: 60 seconds)
        '''

        logger.info('Sending request with tag %s to worker', tag)

        requestFuture = tornado.concurrent.Future()

        # Convert dictionaries and lists to JSON if content type is JSON
        if (isinstance(body, dict) or isinstance(body, list)) \
                and contentType == 'application/json':
            body = json.dumps(body).encode(contentEncoding)

        # If we are expecting a response, generate a request ID
        requestId = expectResponse and uuid4().hex or None

        # Delegate actual sending to subclass
        requestResult = self._request(
                requestId=requestId,
                tag=tag,
                body=body,
                contentType=contentType,
                contentEncoding=contentEncoding,
                **kwargs)

        if requestResult is not None:
            self._ioloop.add_future(requestResult,
                    partial(self._onRequestSent, requestFuture,
                        expectResponse, requestId, timeout))
        else:
            self._onRequestSent(requestFuture, expectResponse, requestId,
                    timeout, None)

        # If we are expecting a response, we store our future for some time
        if expectResponse:
            assert requestId is not None
            Producer._requestFutures[requestId] = requestFuture

            # If our future is still stored after a while, remove it and
            # set an error on it
            # TODO: Remove timeout on successful response
            def removeFuture():
                if requestId in Producer._requestFutures:
                    logger.info('Worker request timed out')
                    requestFuture.set_exception(ResponseTimeout())
                    del Producer._requestFutures[requestId]
            self._ioloop.add_timeout(timeout, removeFuture)

        return requestFuture


    # pylint: disable=too-many-arguments
    def _request(self, requestId, tag, body, contentType, contentEncoding,
            **kwargs):
        '''
        Implementation method for subclasses to send work requests.
        '''
        raise NotImplementedError()


    # pylint: disable=too-many-arguments,unused-argument
    def _onRequestSent(self, requestFuture, expectResponse, requestId,
            timeout, implFuture):
        '''
        When request is sent, store request ID, if set
        '''
        try:
            # Trap exceptions, if any
            if implFuture is not None:
                implFuture.result()
        except Exception as e:  # pylint: disable=broad-except
            requestFuture.set_exception(e)
        else:
            logger.info('Worker request sent')
            if not expectResponse:
                # Not expecting a response and the request has been sent, so
                # resolve the request future
                requestFuture.set_result(None)


    def _onResponse(self, requestId, body, contentType, contentEncoding):
        '''
        Resolve the request with the given ID.

        This should be called by subclasses when they receive a response.
        '''
        logger.info('Response received for request %s (partial): %s',
                requestId, body[0:100])
        try:
            requestFuture = Producer._requestFutures[requestId]
        except KeyError:
            logger.info('No known request with ID %s (maybe timed out)',
                    requestId)
        else:
            # Convert body to dictionary from JSON if content type is JSON
            if contentType == 'application/json':
                response = json.loads(body.decode(contentEncoding))

                # Set exception if error returned
                if isinstance(response, dict):
                    error = response.pop('error', None)
                    if error is not None:
                        requestFuture.set_exception(
                                WorkerException(error, **response))
                    else:
                        # No error, so set result
                        requestFuture.set_result(response)
                else:
                    requestFuture.set_result(response)
            else:
                response = \
                {
                    'contentType': contentType,
                    'contentEncoding': contentEncoding,
                    'body': body
                }
                requestFuture.set_result(response)
            del Producer._requestFutures[requestId]


    # Non-public instance attributes
    _ioloop = None


    # Non-public class attributes
    _requestFutures = {}

futureClasses = (tornado.concurrent.Future, asyncio.Future,
        concurrent.futures.Future)

class Consumer(WorkerInterface):
    '''
    Abstract base class for workers.
    '''

    def __init__(self, handlers, ioloop=None):
        self._handlers = [tornado.web.URLSpec(*spec) for spec in handlers]
        self._ioloop = ioloop is not None and ioloop or IOLoop.current()


    # pylint: disable=too-many-arguments
    def respond(self, requestId, replyTo, body, contentType, contentEncoding):
        raise NotImplementedError()


    # pylint: disable=too-many-arguments
    def _onRequest(self, requestId, tag, body, contentType, contentEncoding,
            replyTo):
        '''
        Callback for handling messages, called by subclasses.
        '''
        logger.info('Request received for tag %s (may be partial): %s',
                tag, body[0:50])
        logger.debug('Request ID: %s', requestId)
        logger.debug('Request body (may be partial): %s', body[0:1000])
        try:
            # Convert body to dictionary from JSON if content type is JSON
            if contentType == 'application/json':
                body = json.loads(body.decode(contentEncoding))

            # Find handler for the given work tag
            handler, args, kwargs = self._findHandler(
                    requestId, tag, body, contentType, contentEncoding)
            if handler is None:
                raise WorkerException('No handler found for tag %s' % (tag,))

            # Call the work handler
            result = handler(*args, **kwargs)

        except Exception as e:  # pylint: disable=broad-except
            trace = traceback.format_exc()
            logging.error(trace)

            # If response expected, return an error response
            if requestId is not None:
                response = dict(error=str(e))
                if isinstance(e, WorkerException):
                    response['tag'] = e.tag
                    response.update(e.getData())

                self.respond(requestId, replyTo,
                        json.dumps(response).encode('utf-8'),
                        'application/json', 'utf-8')
        else:
            # If no request ID, don't do anything
            if requestId is None:
                return

            # Respond when the worker operation is complete
            if isinstance(result, futureClasses) or asyncio.iscoroutine(result):
                # Convert coroutine to future
                if asyncio.iscoroutine(result):
                    result = asyncio.ensure_future(result)
                self._ioloop.add_future(result,
                        partial(self._respond, requestId, replyTo))
            else:
                resultFuture = tornado.concurrent.Future()
                resultFuture.set_result(result)
                self._respond(requestId, replyTo, resultFuture)


    # pylint: disable=too-many-arguments
    def _findHandler(self, requestId, tag, body, contentType, contentEncoding):
        # Find handler that matches the given tag. This code is mostly copied
        # from Tornado v3.2's URL spec matching in tornado.web.Application.
        handler = None
        args = []
        kwargs = {}
        for spec in self._handlers:
            match = spec.regex.match(tag)
            if match:
                handler = spec.handler_class(
                    WorkRequest(requestId, tag, body, contentType,
                        contentEncoding), **spec.kwargs)
                if spec.regex.groups:
                    # None-safe wrapper around url_unescape to handle
                    # unmatched optional groups correctly
                    def unquote(s):
                        if s is None:
                            return s
                        return escape.url_unescape(s, encoding=None,
                                                   plus=False)
                    # Pass matched groups to the handler.  Since
                    # match.groups() includes both named and unnamed groups,
                    # we want to use either groups or groupdict but not both.
                    # Note that args are passed as bytes so the handler can
                    # decide what encoding to use.

                    if spec.regex.groupindex:
                        kwargs = dict(
                            (str(k), unquote(v))
                            for (k, v) in list(match.groupdict().items()))
                    else:
                        args = [unquote(s) for s in match.groups()]
                break

        return handler, args, kwargs


    def _respond(self, requestId, replyTo, resultFuture):
        try:
            result = resultFuture.result()
        except Exception as e:  # pylint: disable=broad-except
            trace = traceback.format_exc()
            logging.error(trace)

            response = dict(error=str(e))
            if isinstance(e, WorkerException):
                response['tag'] = e.tag
                response.update(e.getData())

            self.respond(requestId, replyTo,
                    json.dumps(response).encode('utf-8'),
                    'application/json', 'utf-8')
        else:
            # Respond with the worker's result
            if result is None or isinstance(result, dict) \
                    or isinstance(result, list):
                try:
                    result = json.dumps(result).encode('utf-8')
                except Exception as e:  # pylint: disable=broad-except
                    result = json.dumps(dict(error=str(e))).encode('utf-8')
                contentType = 'application/json'
                contentEncoding = 'utf-8'
            elif isinstance(result, tuple):
                result, contentType, contentEncoding = result
            else:
                # Other return values not supported
                logging.warning('Result value of type %s not supported',
                        str(type(result)))
                self.respond(requestId, replyTo, json.dumps(
                    dict(error='Worker consumer error: ' +
                        'unsupported result type')).encode(
                            'utf-8'), 'application/json', 'utf-8')
                return

            self.respond(requestId, replyTo, result, contentType,
                    contentEncoding)


    # Non-public instance attributes
    _handlers = None
    _ioloop = None


class WorkRequest(object):
    # Public instance attributes
    id = None
    tag = None
    body = None
    contentType = None
    contentEncoding = None


    # pylint: disable=too-many-arguments
    def __init__(self, requestId, tag, body, contentType, contentEncoding):
        self.id = requestId
        self.tag = tag
        self.body = body
        self.contentType = contentType
        self.contentEncoding = contentEncoding


class WorkHandlerCfgError(Exception):
    pass


class WorkHandler(object):
    context = None
    request = None

    def __init__(self, request, **kwargs):
        self.request = request

        # Initialize context with arguments
        self.context = kwargs

        # Set shortcut attributes on this object
        for key in self.context['shortcutAttrs']:
            setattr(self, key, self.context.get(key))


    def __call__(self):
        '''
        Execution method for the worker.

        Implement this with your own domain logic. Any return value from
        this method will be sent back to the requestor, but only if it is
        expecting a response. If the operation this method performs is
        asynchronous, return a future.
        '''
        raise NotImplementedError()
