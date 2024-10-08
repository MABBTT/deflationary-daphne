import collections
import time
from urllib import parse

import http_strategies
from http_base import DaphneTestCase, DaphneTestingInstance
from hypothesis import given, settings

from daphne.testing import BaseDaphneTestingInstance


class TestWebsocket(DaphneTestCase):
    """
    Tests WebSocket handshake, send and receive.
    """

    def assert_valid_websocket_scope(
        self, scope, path="/", params=None, headers=None, scheme=None, subprotocols=None
    ):
        """
        Checks that the passed scope is a valid ASGI HTTP scope regarding types
        and some urlencoding things.
        """
        # Check overall keys
        self.assert_key_sets(
            required_keys={
                "asgi",
                "type",
                "path",
                "raw_path",
                "query_string",
                "headers",
            },
            optional_keys={"scheme", "root_path", "client", "server", "subprotocols"},
            actual_keys=scope.keys(),
        )
        self.assertEqual(scope["asgi"]["version"], "3.0")
        # Check that it is the right type
        self.assertEqual(scope["type"], "websocket")
        # Path
        self.assert_valid_path(scope["path"])
        # Scheme
        self.assertIn(scope.get("scheme", "ws"), ["ws", "wss"])
        if scheme:
            self.assertEqual(scheme, scope["scheme"])
        # Query string (byte string and still url encoded)
        query_string = scope["query_string"]
        self.assertIsInstance(query_string, bytes)
        if params:
            self.assertEqual(
                query_string, parse.urlencode(params or []).encode("ascii")
            )
        # Ordering of header names is not important, but the order of values for a header
        # name is. To assert whether that order is kept, we transform both the request
        # headers and the channel message headers into a dictionary
        # {name: [value1, value2, ...]} and check if they're equal.
        transformed_scope_headers = collections.defaultdict(list)
        for name, value in scope["headers"]:
            transformed_scope_headers.setdefault(name, [])
            # Make sure to split out any headers collapsed with commas
            for bit in value.split(b","):
                if bit.strip():
                    transformed_scope_headers[name].append(bit.strip())
        transformed_request_headers = collections.defaultdict(list)
        for name, value in headers or []:
            expected_name = name.lower().strip()
            expected_value = value.strip()
            # Make sure to split out any headers collapsed with commas
            transformed_request_headers.setdefault(expected_name, [])
            for bit in expected_value.split(b","):
                if bit.strip():
                    transformed_request_headers[expected_name].append(bit.strip())
        for name, value in transformed_request_headers.items():
            self.assertIn(name, transformed_scope_headers)
            self.assertEqual(value, transformed_scope_headers[name])
        # Root path
        self.assertIsInstance(scope.get("root_path", ""), str)
        # Client and server addresses
        client = scope.get("client")
        if client is not None:
            self.assert_valid_address_and_port(client)
        server = scope.get("server")
        if server is not None:
            self.assert_valid_address_and_port(server)
        # Subprotocols
        scope_subprotocols = scope.get("subprotocols", [])
        if scope_subprotocols:
            assert all(isinstance(x, str) for x in scope_subprotocols)
        if subprotocols:
            assert sorted(scope_subprotocols) == sorted(subprotocols)

    def assert_valid_websocket_connect_message(self, message):
        """
        Asserts that a message is a valid http.request message
        """
        # Check overall keys
        self.assert_key_sets(
            required_keys={"type"}, optional_keys=set(), actual_keys=message.keys()
        )
        # Check that it is the right type
        self.assertEqual(message["type"], "websocket.connect")

    def test_accept(self):
        """
        Tests we can open and accept a socket.
        """
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages([{"type": "websocket.accept"}])
            self.websocket_handshake(test_app)
            # Validate the scope and messages we got
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_scope(scope)
            self.assert_valid_websocket_connect_message(messages[0])

    def test_reject(self):
        """
        Tests we can reject a socket and it won't complete the handshake.
        """
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages([{"type": "websocket.close"}])
            with self.assertRaises(RuntimeError):
                self.websocket_handshake(test_app)

    def test_subprotocols(self):
        """
        Tests that we can ask for subprotocols and then select one.
        """
        subprotocols = ["proto1", "proto2"]
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages(
                [{"type": "websocket.accept", "subprotocol": "proto2"}]
            )
            _, subprotocol = self.websocket_handshake(
                test_app, subprotocols=subprotocols
            )
            # Validate the scope and messages we got
            assert subprotocol == "proto2"
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_scope(scope, subprotocols=subprotocols)
            self.assert_valid_websocket_connect_message(messages[0])

    def test_accept_permessage_deflate_extension(self):
        """
        Tests that permessage-deflate extension is successfuly accepted
        by underlying `autobahn` package.
        """

        headers = [
            (
                b"Sec-WebSocket-Extensions",
                b"permessage-deflate; client_max_window_bits",
            ),
        ]

        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages(
                [
                    {
                        "type": "websocket.accept",
                    }
                ]
            )

            sock, subprotocol = self.websocket_handshake(
                test_app,
                headers=headers,
            )
            # Validate the scope and messages we got
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_connect_message(messages[0])

    def test_accept_custom_extension(self):
        """
        Tests that custom headers can be accpeted during handshake.
        """
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages(
                [
                    {
                        "type": "websocket.accept",
                        "headers": [(b"Sec-WebSocket-Extensions", b"custom-extension")],
                    }
                ]
            )

            sock, subprotocol = self.websocket_handshake(
                test_app,
                headers=[
                    (b"Sec-WebSocket-Extensions", b"custom-extension"),
                ],
            )
            # Validate the scope and messages we got
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_connect_message(messages[0])

    def test_xff(self):
        """
        Tests that X-Forwarded-For headers get parsed right
        """
        headers = [["X-Forwarded-For", "10.1.2.3"], ["X-Forwarded-Port", "80"]]
        with DaphneTestingInstance(xff=True) as test_app:
            test_app.add_send_messages([{"type": "websocket.accept"}])
            self.websocket_handshake(test_app, headers=headers)
            # Validate the scope and messages we got
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_scope(scope)
            self.assert_valid_websocket_connect_message(messages[0])
            assert scope["client"] == ["10.1.2.3", 80]

    @given(
        request_path=http_strategies.http_path(),
        request_params=http_strategies.query_params(),
        request_headers=http_strategies.headers(),
    )
    @settings(max_examples=5, deadline=2000)
    def test_http_bits(self, request_path, request_params, request_headers):
        """
        Tests that various HTTP-level bits (query string params, path, headers)
        carry over into the scope.
        """
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages([{"type": "websocket.accept"}])
            self.websocket_handshake(
                test_app,
                path=parse.quote(request_path),
                params=request_params,
                headers=request_headers,
            )
            # Validate the scope and messages we got
            scope, messages = test_app.get_received()
            self.assert_valid_websocket_scope(
                scope, path=request_path, params=request_params, headers=request_headers
            )
            self.assert_valid_websocket_connect_message(messages[0])

    def test_raw_path(self):
        """
        Tests that /foo%2Fbar produces raw_path and a decoded path
        """
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages([{"type": "websocket.accept"}])
            self.websocket_handshake(test_app, path="/foo%2Fbar")
            # Validate the scope and messages we got
            scope, _ = test_app.get_received()

        self.assertEqual(scope["path"], "/foo/bar")
        self.assertEqual(scope["raw_path"], b"/foo%2Fbar")

    @given(daphne_path=http_strategies.http_path())
    @settings(max_examples=5, deadline=2000)
    def test_root_path(self, *, daphne_path):
        """
        Tests root_path handling.
        """
        headers = [("Daphne-Root-Path", parse.quote(daphne_path))]
        with DaphneTestingInstance() as test_app:
            test_app.add_send_messages([{"type": "websocket.accept"}])
            self.websocket_handshake(
                test_app,
                path="/",
                headers=headers,
            )
            # Validate the scope and messages we got
            scope, _ = test_app.get_received()

        # Daphne-Root-Path is not included in the returned 'headers' section.
        self.assertNotIn(
            "daphne-root-path", (header[0].lower() for header in scope["headers"])
        )
        # And what we're looking for, root_path being set.
        self.assertEqual(scope["root_path"], daphne_path)

    def test_text_frames(self):
        """
        Tests we can send and receive text frames.
        """
        with DaphneTestingInstance() as test_app:
            # Connect
            test_app.add_send_messages([{"type": "websocket.accept"}])
            sock, _ = self.websocket_handshake(test_app)
            _, messages = test_app.get_received()
            self.assert_valid_websocket_connect_message(messages[0])
            # Prep frame for it to send
            test_app.add_send_messages(
                [{"type": "websocket.send", "text": "here be dragons 🐉"}]
            )
            # Send it a frame
            self.websocket_send_frame(sock, "what is here? 🌍")
            # Receive a frame and make sure it's correct
            assert self.websocket_receive_frame(sock) == "here be dragons 🐉"
            # Make sure it got our frame
            _, messages = test_app.get_received()
            assert messages[1] == {
                "type": "websocket.receive",
                "text": "what is here? 🌍",
            }

    def test_binary_frames(self):
        """
        Tests we can send and receive binary frames with things that are very
        much not valid UTF-8.
        """
        with DaphneTestingInstance() as test_app:
            # Connect
            test_app.add_send_messages([{"type": "websocket.accept"}])
            sock, _ = self.websocket_handshake(test_app)
            _, messages = test_app.get_received()
            self.assert_valid_websocket_connect_message(messages[0])
            # Prep frame for it to send
            test_app.add_send_messages(
                [{"type": "websocket.send", "bytes": b"here be \xe2 bytes"}]
            )
            # Send it a frame
            self.websocket_send_frame(sock, b"what is here? \xe2")
            # Receive a frame and make sure it's correct
            assert self.websocket_receive_frame(sock) == b"here be \xe2 bytes"
            # Make sure it got our frame
            _, messages = test_app.get_received()
            assert messages[1] == {
                "type": "websocket.receive",
                "bytes": b"what is here? \xe2",
            }

    def test_http_timeout(self):
        """
        Tests that the HTTP timeout doesn't kick in for WebSockets
        """
        with DaphneTestingInstance(http_timeout=1) as test_app:
            # Connect
            test_app.add_send_messages([{"type": "websocket.accept"}])
            sock, _ = self.websocket_handshake(test_app)
            _, messages = test_app.get_received()
            self.assert_valid_websocket_connect_message(messages[0])
            # Wait 2 seconds
            time.sleep(2)
            # Prep frame for it to send
            test_app.add_send_messages([{"type": "websocket.send", "text": "cake"}])
            # Send it a frame
            self.websocket_send_frame(sock, "still alive?")
            # Receive a frame and make sure it's correct
            assert self.websocket_receive_frame(sock) == "cake"

    def test_application_checker_handles_asyncio_cancellederror(self):
        with CancellingTestingInstance() as app:
            # Connect to the websocket app, it will immediately raise
            # asyncio.CancelledError
            sock, _ = self.websocket_handshake(app)
            # Disconnect from the socket
            sock.close()
            # Wait for application_checker to clean up the applications for
            # disconnected clients, and for the server to be stopped.
            time.sleep(3)
            # Make sure we received either no error, or a ConnectionsNotEmpty
            while not app.process.errors.empty():
                err, _tb = app.process.errors.get()
                if not isinstance(err, ConnectionsNotEmpty):
                    raise err
                self.fail(
                    "Server connections were not cleaned up after an asyncio.CancelledError was raised"
                )


async def cancelling_application(scope, receive, send):
    import asyncio

    from twisted.internet import reactor

    # Stop the server after a short delay so that the teardown is run.
    reactor.callLater(2, reactor.stop)
    await send({"type": "websocket.accept"})
    raise asyncio.CancelledError()


class ConnectionsNotEmpty(Exception):
    pass


class CancellingTestingInstance(BaseDaphneTestingInstance):
    def __init__(self):
        super().__init__(application=cancelling_application)

    def process_teardown(self):
        import multiprocessing

        # Get a hold of the enclosing DaphneProcess (we're currently running in
        # the same process as the application).
        proc = multiprocessing.current_process()
        # By now the (only) socket should have disconnected, and the
        # application_checker should have run. If there are any connections
        # still, it means that the application_checker did not clean them up.
        if proc.server.connections:
            raise ConnectionsNotEmpty()
