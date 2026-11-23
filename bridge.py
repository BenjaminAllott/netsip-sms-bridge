#!/usr/bin/env python3
# Copyright (C) 2026 Allott Group Pty Ltd
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3. It is distributed WITHOUT ANY WARRANTY; see
# the LICENSE file for details.
#
# Source: https://github.com/BenjaminAllott/netsip-sms-bridge
# SPDX-License-Identifier: AGPL-3.0-only
"""SMS bridge between Over the Wire / NetSIP and a Yeastar P-Series PBX.

The bridge plays the "Service Provider" role of Yeastar's generic SMS API
(Messaging > Message Channel > SMS > ITSP "General"):

  PBX  --POST /yeastar/send-->  bridge  --POST /api/v1/service/sms/-->  NetSIP
  PBX  <--webhook (signed)----  bridge  <--poll /sms/inbound/ or callback--  NetSIP

Python standard library only. Configuration is by environment variables,
see .env.example.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("sms-bridge")

MAX_BODY = 1024 * 1024
SEEN_RETENTION = 7 * 24 * 3600

# Field names NetSIP may use. The inbound list / callback payload formats are
# not in the API document, so these are matched tolerantly.
ID_KEYS = ("id", "sms_id", "message_id", "msg_id", "uuid")
FROM_KEYS = ("from_addr", "from", "source", "src", "sender")
TO_KEYS = ("to_addr", "to", "destination", "dst", "recipient")
TEXT_KEYS = ("content", "message", "text", "body", "msg")
DATE_KEYS = ("date", "received", "received_at", "created", "created_at", "timestamp")
LIST_KEYS = ("results", "objects", "data", "messages", "items", "sms", "inbound")


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        source = env if env is not None else os.environ

        def g(key, default=""):
            # A blank value (e.g. an empty field in a container template) means "use the default".
            return (source.get(key) or "").strip() or default

        self.port = int(g("LISTEN_PORT", "8080"))
        self.yeastar_api_key = g("YEASTAR_API_KEY", "")
        self.yeastar_webhook_url = g("YEASTAR_WEBHOOK_URL", "")
        self.yeastar_secret = g("YEASTAR_SECRET", "")
        self.netsip_base = g("NETSIP_BASE_URL", "https://api.overthewire.com.au").rstrip("/")
        self.netsip_account = g("NETSIP_ACCOUNT_ID", "")
        self.netsip_user = g("NETSIP_USERNAME", "")
        self.netsip_pass = g("NETSIP_PASSWORD", "")
        self.netsip_plus = truthy(g("NETSIP_PLUS_PREFIX", "true"))
        self.country_code = g("DEFAULT_COUNTRY_CODE", "61").lstrip("+")
        self.inbound_mode = g("INBOUND_MODE", "poll").strip().lower()
        self.poll_interval = max(5, int(g("POLL_INTERVAL", "15")))
        self.poll_numbers = [n.strip() for n in g("NETSIP_NUMBERS", "").split(",") if n.strip()]
        self.public_url = g("PUBLIC_BASE_URL", "").rstrip("/")
        self.callback_token = g("CALLBACK_TOKEN", "")
        self.state_file = g("STATE_FILE", "/data/state.json")
        self.timeout = int(g("HTTP_TIMEOUT", "20"))

    def problems(self):
        out = []
        if not self.yeastar_api_key:
            out.append("YEASTAR_API_KEY is required")
        if not (self.netsip_account and self.netsip_user and self.netsip_pass):
            out.append("NETSIP_ACCOUNT_ID, NETSIP_USERNAME and NETSIP_PASSWORD are required")
        if self.inbound_mode not in ("poll", "callback", "off"):
            out.append("INBOUND_MODE must be poll, callback or off")
        if self.inbound_mode != "off" and not self.yeastar_webhook_url:
            out.append("YEASTAR_WEBHOOK_URL is required unless INBOUND_MODE=off")
        if self.inbound_mode == "callback" and not (self.public_url and self.callback_token):
            out.append("INBOUND_MODE=callback needs PUBLIC_BASE_URL and CALLBACK_TOKEN")
        return out


def to_e164(number, country_code="61"):
    """Normalise a phone number to +E.164. Alphanumeric sender IDs pass through."""
    raw = str(number or "").strip()
    if any(c.isalpha() for c in raw):
        return raw
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + country_code + digits[1:]
    return "+" + digits


def pick(item, keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return ""


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def iso_from(value):
    """Use the provider timestamp when it is unambiguous, otherwise now."""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.isoformat(timespec="milliseconds")
    except (ValueError, TypeError):
        pass
    return iso_now()


def http_request(method, url, headers=None, body=None, timeout=20):
    """Return (status, text). Raises OSError when the server is unreachable."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class State:
    """Remembers which inbound message IDs were already delivered to the PBX."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.seen = {}
        self.primed = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.seen = dict(data.get("seen", {}))
            self.primed = True
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            log.warning("could not read state file %s (%s); starting fresh", path, e)

    def has(self, key):
        with self.lock:
            return key in self.seen

    def add(self, key):
        with self.lock:
            self.seen[key] = time.time()

    def save(self):
        with self.lock:
            cutoff = time.time() - SEEN_RETENTION
            self.seen = {k: t for k, t in self.seen.items() if t >= cutoff}
            self.primed = True
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"seen": self.seen}, f)
                os.replace(tmp, self.path)
            except OSError as e:
                log.error("could not write state file %s: %s", self.path, e)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State(cfg.state_file)
        self.logged_sample = False

    # ---------- NetSIP side ----------

    def netsip_number(self, number):
        n = to_e164(number, self.cfg.country_code)
        return n if self.cfg.netsip_plus else n.lstrip("+")

    def netsip(self, method, path, params=None, body=None):
        url = self.cfg.netsip_base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        cred = ("%s:%s" % (self.cfg.netsip_user, self.cfg.netsip_pass)).encode()
        headers = {"Authorization": "Basic " + base64.b64encode(cred).decode(),
                   "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        return http_request(method, url, headers, data, self.cfg.timeout)

    # ---------- PBX -> NetSIP ----------

    def send_from_yeastar(self, msg):
        """Handle Yeastar's 'send message' request. Returns (http_status, json_body)."""
        frm, to = msg.get("from"), msg.get("to")
        text = msg.get("text") or ""
        if not frm or not to:
            return 400, err("10002", "Invalid parameter", "'from' and 'to' are required.")
        if not text:
            code = "10003" if msg.get("media_urls") else "10002"
            return 400, err(code, "Unsupported message", "Only text SMS is supported (no MMS).")
        to_n = self.netsip_number(to)
        if not to_n.lstrip("+").isdigit():
            return 400, err("10001", "Invalid 'to' address", "The 'to' address must be a phone number.")

        body = {"account_id": self.cfg.netsip_account, "to_addr": to_n,
                "from_addr": self.netsip_number(frm), "content": text}
        if self.cfg.inbound_mode == "callback":
            body["callback_reply_uri"] = "%s/netsip/inbound?token=%s" % (
                self.cfg.public_url, urllib.parse.quote(self.cfg.callback_token))
            body["callback_reply_method"] = "httprest"
        try:
            status, resp = self.netsip("POST", "/api/v1/service/sms/", body=body)
        except OSError as e:
            log.error("send %s -> %s: NetSIP unreachable: %s", frm, to, e)
            return 500, err("10007", "Service unavailable", "NetSIP API unreachable: %s" % e)

        if 200 <= status < 300:
            msg_id = ""
            try:
                parsed = json.loads(resp)
                if isinstance(parsed, dict):
                    msg_id = str(pick(parsed, ID_KEYS))
            except ValueError:
                pass
            # Yeastar requires an ID that is different for every message.
            msg_id = "%s-%s" % (msg_id, uuid.uuid4().hex[:8]) if msg_id else str(uuid.uuid4())
            log.info("sent %s -> %s (%d chars) id=%s", frm, to, len(text), msg_id)
            return 200, {"data": {"id": msg_id}}

        log.error("send %s -> %s: NetSIP returned %s: %s", frm, to, status, resp[:500])
        if status in (401, 403):
            return 401, err("10004", "Authentication failed", "NetSIP rejected the bridge credentials.")
        if status == 429:
            return 429, err("10006", "Too many requests", resp[:300])
        if status == 400:
            return 400, err("10002", "Rejected by NetSIP", resp[:300])
        return 500, err("10007", "NetSIP error %s" % status, resp[:300])

    # ---------- NetSIP -> PBX ----------

    def deliver_to_yeastar(self, msg_id, frm, to, text, when=None):
        """POST an inbound message to the PBX webhook. Returns True on success."""
        ts = iso_from(when)
        payload = {"data": {
            "event_type": "message.received",
            "id": str(uuid.uuid4()),
            "occurred_at": ts,
            "record_type": "event",
            "payload": {
                "id": msg_id[:255],
                "direction": "inbound",
                "type": "SMS",
                "record_type": "message",
                "from": {"phone_number": to_e164(frm, self.cfg.country_code)},
                "to": [{"phone_number": to_e164(to, self.cfg.country_code)}],
                "text": text,
                "media": [],
                "received_at": ts,
            }}}
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.cfg.yeastar_secret:
            sig = hmac.new(self.cfg.yeastar_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Signature-256"] = "sha256=" + sig
        try:
            status, resp = http_request("POST", self.cfg.yeastar_webhook_url, headers, body, self.cfg.timeout)
        except OSError as e:
            log.error("inbound %s -> %s: PBX webhook unreachable: %s", frm, to, e)
            return False
        if 200 <= status < 300:
            log.info("inbound %s -> %s (%d chars) delivered to PBX id=%s", frm, to, len(text), msg_id)
            return True
        log.error("inbound %s -> %s: PBX webhook returned %s: %s", frm, to, status, resp[:300])
        return False

    def fetch_inbound(self, to_addr=None):
        items = []
        date_from = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        for page in range(20):
            params = {"account_id": self.cfg.netsip_account, "date_from": date_from,
                      "limit": 100, "offset": page * 100}
            if to_addr:
                params["to_addr"] = self.netsip_number(to_addr)
            status, resp = self.netsip("GET", "/api/v1/service/sms/inbound/", params=params)
            if not 200 <= status < 300:
                raise OSError("NetSIP inbound list returned %s: %s" % (status, resp[:300]))
            if not self.logged_sample:
                self.logged_sample = True
                log.info("first inbound list response (truncated): %s", resp[:600])
            data = json.loads(resp)
            rows = data
            if isinstance(data, dict):
                rows = next((data[k] for k in LIST_KEYS if isinstance(data.get(k), list)), None)
                if rows is None:
                    rows = next((v for v in data.values() if isinstance(v, list)), [])
            rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            items.extend(rows)
            if len(rows) < 100:
                break
        return items

    def poll_once(self):
        items = []
        for number in (self.cfg.poll_numbers or [None]):
            items.extend(self.fetch_inbound(number))
        first_run = not self.state.primed
        delivered = 0
        for item in items:
            raw_id = pick(item, ID_KEYS)
            key = str(raw_id) if raw_id != "" else hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()).hexdigest()
            if self.state.has(key):
                continue
            if first_run:
                # Do not replay history into the PBX the first time we start.
                self.state.add(key)
                continue
            frm, to, text = pick(item, FROM_KEYS), pick(item, TO_KEYS), pick(item, TEXT_KEYS)
            if not (frm and to and text):
                log.warning("skipping inbound item with unrecognised fields: %s", json.dumps(item)[:400])
                self.state.add(key)
                continue
            if self.deliver_to_yeastar("netsip-" + key, frm, to, str(text), pick(item, DATE_KEYS)):
                self.state.add(key)
                delivered += 1
        if first_run:
            log.info("first run: marked %d existing inbound messages as already seen", len(items))
        self.state.save()
        return delivered

    def poll_forever(self):
        log.info("polling NetSIP inbound every %ss", self.cfg.poll_interval)
        while True:
            try:
                self.poll_once()
            except Exception as e:  # keep the poller alive whatever happens
                log.error("poll failed: %s", e)
            time.sleep(self.cfg.poll_interval)

    def handle_callback(self, fields):
        frm, to, text = pick(fields, FROM_KEYS), pick(fields, TO_KEYS), pick(fields, TEXT_KEYS)
        if not (frm and to and text):
            log.warning("callback with unrecognised fields: %s", json.dumps(fields)[:400])
            return False
        raw_id = pick(fields, ID_KEYS)
        msg_id = "netsip-%s" % raw_id if raw_id != "" else str(uuid.uuid4())
        return self.deliver_to_yeastar(msg_id, frm, to, str(text), pick(fields, DATE_KEYS))


def err(code, title, detail):
    return {"errors": [{"code": code, "title": title, "detail": detail}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "sms-bridge"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def reply(self, status, body=None, content_type="application/json"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        else:
            data = (body or "").encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def pbx_authorised(self):
        key = self.server.bridge.cfg.yeastar_api_key
        given = self.headers.get("Authorization", "")
        return bool(key) and hmac.compare_digest(given.encode(), ("Bearer " + key).encode())

    def route(self, method):
        bridge = self.server.bridge
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        body = self.read_body()
        if body is None:
            return self.reply(413, err("10008", "Too large", "Request body too large."))

        if path == "/healthz":
            return self.reply(200, {"status": "ok"})

        if path == "/yeastar/verify" and method == "GET":
            if not self.pbx_authorised():
                return self.reply(401, err("10004", "Authentication failed", "Invalid API key."))
            return self.reply(200, query.get("challenge", ""), "text/plain")

        if path == "/yeastar/send" and method == "POST":
            if not self.pbx_authorised():
                return self.reply(401, err("10004", "Authentication failed", "Invalid API key."))
            try:
                msg = json.loads(body or b"{}")
                if not isinstance(msg, dict):
                    raise ValueError
            except ValueError:
                return self.reply(400, err("10002", "Invalid parameter", "Body must be a JSON object."))
            status, out = bridge.send_from_yeastar(msg)
            return self.reply(status, out)

        if path == "/netsip/inbound":
            cfg = bridge.cfg
            token = query.pop("token", "")
            if cfg.inbound_mode != "callback" or not hmac.compare_digest(
                    token.encode(), cfg.callback_token.encode()):
                return self.reply(403, {"error": "forbidden"})
            fields = dict(query)
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        fields.update(parsed)
                except ValueError:
                    form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                    fields.update({k: v[0] for k, v in form.items()})
            log.info("NetSIP callback received: %s", json.dumps(fields)[:600])
            ok = bridge.handle_callback(fields)
            return self.reply(200 if ok else 502, {"status": "ok" if ok else "not delivered"})

        return self.reply(404, {"error": "not found"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


def make_server(cfg, host="0.0.0.0"):
    server = ThreadingHTTPServer((host, cfg.port), Handler)
    server.daemon_threads = True
    server.bridge = Bridge(cfg)
    return server


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    problems = cfg.problems()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        raise SystemExit(2)
    server = make_server(cfg)
    if cfg.inbound_mode == "poll":
        threading.Thread(target=server.bridge.poll_forever, daemon=True).start()
    log.info("listening on :%d (inbound mode: %s)", cfg.port, cfg.inbound_mode)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
