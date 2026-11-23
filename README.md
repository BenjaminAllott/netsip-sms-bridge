# NetSIP / Over the Wire ⇄ Yeastar SMS bridge

A single-file Python service (standard library only) that lets a Yeastar P-Series PBX
send and receive SMS through the Over the Wire / NetSIP SMS API. It presents itself to
the PBX as a generic SMS provider (ITSP **General**).

```
Linkus user ─► PBX ─► POST /yeastar/send ─► bridge ─► POST /api/v1/service/sms/ ─► NetSIP ─► handset
Linkus user ◄─ PBX ◄─ signed webhook ◄───── bridge ◄─ GET /api/v1/service/sms/inbound/ (poll) ◄─ NetSIP
```

## Requirements

- Yeastar P-Series with the generic SMS API: firmware 37.23.0.24+ (appliance),
  83.23.0.24+ (software), or Cloud Edition.
- The PBX must reach the bridge over **HTTPS with a valid certificate**, and the bridge
  must reach the PBX webhook URL and `api.overthewire.com.au`.
- A NetSIP account ID (`SRCxxxxx`) and API username/password with SMS enabled.

## Run it

The quickest way is the published image. Create a `.env` from
[.env.example](.env.example), then:

```
docker run -d --name netsip-sms --restart unless-stopped \
  --env-file .env -v netsip-sms-data:/data -p 127.0.0.1:8080:8080 \
  sparkybearbomb/netsip-sms-bridge:latest
docker logs -f netsip-sms
```

The same image is also published as `ghcr.io/benjaminallott/netsip-sms-bridge:latest`.
Update later with `docker pull sparkybearbomb/netsip-sms-bridge:latest`, then remove
and re-run the container.

Or build from source:

```
cp .env.example .env      # fill it in
docker compose up -d --build
docker compose logs -f sms-bridge
```

The container listens on `127.0.0.1:8080`; point your reverse proxy at it. If you have
no proxy, set `BRIDGE_DOMAIN` and run `docker compose --profile tls up -d --build` to
get HTTPS from the bundled Caddy.

No Docker? It is one file: set the same variables in the environment and run
`python bridge.py` (Python 3.9+), with `STATE_FILE` pointing somewhere writable.

## PBX setup

Messaging > Message Channel > Add > SMS:

| Field | Value |
| --- | --- |
| ITSP | General |
| API Key | the value of `YEASTAR_API_KEY` |
| Secret | the value of `YEASTAR_SECRET` |
| API Address for Sending Messages | `https://<bridge host>/yeastar/send` |
| API Address for Verifying Authentication | `https://<bridge host>/yeastar/verify` |
| Webhook URL | copy it into `YEASTAR_WEBHOOK_URL`, then restart the bridge |

Then add your NetSIP number(s) in E.164 form (`+61...`) under **Number** and pick where
inbound messages go.

## Inbound modes

- `poll` (default): the bridge lists NetSIP inbound messages every `POLL_INTERVAL`
  seconds and forwards new ones. Delivered IDs are kept in `/data/state.json`. On the
  very first start, existing messages are marked as seen and not replayed.
- `callback`: each outbound message carries `callback_reply_uri` pointing at
  `PUBLIC_BASE_URL/netsip/inbound?token=CALLBACK_TOKEN` (method `httprest`), and NetSIP
  pushes replies to the bridge. Per the NetSIP document this only covers *replies* to
  messages the bridge sent.

## Things to confirm on first use

The NetSIP document does not show response or callback payloads, so the bridge matches
field names loosely (`from_addr`/`to_addr`/`content`, with fallbacks) and logs what it
gets:

1. Send a test SMS from Linkus. If NetSIP rejects the number format, flip
   `NETSIP_PLUS_PREFIX`.
2. Text the NetSIP number. On the first poll the log prints
   `first inbound list response`; if messages are logged as
   `unrecognised fields`, add the real field names to the `*_KEYS` tuples at the top
   of `bridge.py`.
3. If the PBX shows the channel as *Unauthorized* although sending works, clear the
   "API Address for Verifying Authentication" field (it is optional).

Only plain text SMS is bridged; MMS from the PBX is refused with error 10003.

## Tests

```
python -m unittest -v test_bridge
```

Runs the bridge against in-process mock NetSIP and Yeastar servers.
