"""End-to-end test against in-process mock NetSIP and mock Yeastar servers.

Run:  python -m unittest -v test_bridge
"""
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge


class Mock(BaseHTTPRequestHandler):
    """Acts as both the NetSIP API and the Yeastar webhook."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        s = self.server
        s.requests.append(("GET", self.path, self.headers, b""))
        if self.path.startswith("/api/v1/service/sms/inbound/"):
            return self.send(200, json.dumps({"count": len(s.inbound), "results": s.inbound}))
        self.send(404, "")

    def do_POST(self):
        s = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        s.requests.append(("POST", self.path, self.headers, body))  # case-insensitive lookup
        if self.path == "/api/v1/service/sms/":
            return self.send(s.send_status, json.dumps({"id": 555}))
        if self.path == "/webhook":
            return self.send(204, "")
        self.send(404, "")

    def send(self, status, text):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_address[1]


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        self.mock.requests, self.mock.inbound, self.mock.send_status = [], [], 201
        mock_url = start(self.mock)
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {
            "LISTEN_PORT": "0", "YEASTAR_API_KEY": "pbxkey", "YEASTAR_SECRET": "shh",
            "YEASTAR_WEBHOOK_URL": mock_url + "/webhook", "NETSIP_BASE_URL": mock_url,
            "NETSIP_ACCOUNT_ID": "SRC1", "NETSIP_USERNAME": "u", "NETSIP_PASSWORD": "p",
            "STATE_FILE": os.path.join(self.tmp.name, "state.json"),
        }
        self.boot()

    def boot(self, **extra):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
        self.env.update(extra)
        cfg = bridge.Config(self.env)
        self.assertEqual(cfg.problems(), [])
        self.srv = bridge.make_server(cfg, "127.0.0.1")
        self.url = start(self.srv)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.mock.shutdown()
        self.mock.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, key="pbxkey"):
        headers = {"Authorization": "Bearer " + key}
        data = json.dumps(body).encode() if body is not None else None
        return bridge.http_request(method, self.url + path, headers, data)

    def webhooks(self):
        return [r for r in self.mock.requests if r[1] == "/webhook"]

    def test_verify(self):
        self.assertEqual(self.call("GET", "/yeastar/verify?challenge=abc123"), (200, "abc123"))
        self.assertEqual(self.call("GET", "/yeastar/verify?challenge=x", key="bad")[0], 401)

    def test_send(self):
        status, text = self.call("POST", "/yeastar/send",
                                 {"from": "+61299990000", "to": "0412 345 678", "text": "hi"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(text)["data"]["id"])
        _, path, headers, body = self.mock.requests[-1]
        self.assertEqual(path, "/api/v1/service/sms/")
        self.assertEqual(headers["Authorization"], "Basic dTpw")
        self.assertEqual(json.loads(body), {"account_id": "SRC1", "to_addr": "+61412345678",
                                            "from_addr": "+61299990000", "content": "hi"})

    def test_send_rejections(self):
        self.assertEqual(self.call("POST", "/yeastar/send", {"from": "+61", "to": "+61"}, key="no")[0], 401)
        status, text = self.call("POST", "/yeastar/send",
                                 {"from": "+612", "to": "+614", "media_urls": ["http://x/y"]})
        self.assertEqual((status, json.loads(text)["errors"][0]["code"]), (400, "10003"))
        self.mock.send_status = 401
        status, text = self.call("POST", "/yeastar/send", {"from": "+612", "to": "+614", "text": "x"})
        self.assertEqual((status, json.loads(text)["errors"][0]["code"]), (401, "10004"))

    def test_no_plus_format(self):
        self.boot(NETSIP_PLUS_PREFIX="false")
        self.call("POST", "/yeastar/send", {"from": "+61299990000", "to": "+61412345678", "text": "hi"})
        self.assertEqual(json.loads(self.mock.requests[-1][3])["to_addr"], "61412345678")

    def test_poll(self):
        b = self.srv.bridge
        self.mock.inbound = [{"id": 1, "from_addr": "61400000001", "to_addr": "61299990000", "content": "old"}]
        self.assertEqual(b.poll_once(), 0)          # first run only primes
        self.assertEqual(self.webhooks(), [])
        self.mock.inbound.append({"id": 2, "from_addr": "61400000002", "to_addr": "61299990000",
                                  "content": "new one", "date": "2026-11-23T10:00:00+10:00"})
        self.assertEqual(b.poll_once(), 1)
        self.assertEqual(b.poll_once(), 0)          # not delivered twice
        self.assertEqual(len(self.webhooks()), 1)
        _, _, headers, body = self.webhooks()[0]
        expected = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Signature-256"], "sha256=" + expected)
        p = json.loads(body)["data"]["payload"]
        self.assertEqual(json.loads(body)["data"]["event_type"], "message.received")
        self.assertEqual(p["from"]["phone_number"], "+61400000002")
        self.assertEqual(p["to"][0]["phone_number"], "+61299990000")
        self.assertEqual((p["text"], p["id"]), ("new one", "netsip-2"))
        self.assertEqual(p["received_at"], "2026-11-23T10:00:00.000+10:00")
        # the seen list survives a restart
        self.boot()
        self.assertEqual(self.srv.bridge.poll_once(), 0)

    def test_callback(self):
        q = urllib.parse.urlencode({"token": "tok", "from_addr": "61400000003",
                                    "to_addr": "61299990000", "content": "reply"})
        self.assertEqual(self.call("GET", "/netsip/inbound?" + q)[0], 403)   # poll mode: disabled
        self.boot(INBOUND_MODE="callback", PUBLIC_BASE_URL="https://sms.example.com", CALLBACK_TOKEN="tok")
        self.assertEqual(self.call("GET", "/netsip/inbound?" + q.replace("token=tok", "token=bad"))[0], 403)
        self.assertEqual(self.call("GET", "/netsip/inbound?" + q)[0], 200)
        p = json.loads(self.webhooks()[-1][3])["data"]["payload"]
        self.assertEqual((p["from"]["phone_number"], p["text"]), ("+61400000003", "reply"))
        self.call("POST", "/yeastar/send", {"from": "+612", "to": "+614", "text": "x"})
        sent = json.loads(self.mock.requests[-1][3])
        self.assertEqual(sent["callback_reply_uri"], "https://sms.example.com/netsip/inbound?token=tok")
        self.assertEqual(sent["callback_reply_method"], "httprest")


if __name__ == "__main__":
    unittest.main()
