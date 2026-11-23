# NetSIP SMS Bridge for Yeastar

Send and receive SMS on a **Yeastar P-Series PBX** (Linkus, message queues, message
flows) using **Over the Wire / NetSIP** virtual mobile numbers.

The bridge is a small container that sits between the two. To the PBX it looks like a
generic SMS provider; to NetSIP it is an ordinary API client.

```
Outbound   Linkus user -> PBX -> bridge -> NetSIP SMS API -> handset
Inbound    handset -> NetSIP -> bridge -> PBX webhook -> Linkus user
```

- One Python file, standard library only, on a small Alpine image
- Runs on amd64 and arm64
- Several NetSIP numbers on one PBX channel
- Only touches the numbers you list, so one NetSIP account can serve several PBXs
- No secrets in the image: everything is supplied as environment variables

> Unofficial project, not affiliated with or supported by Over the Wire, NetSIP or
> Yeastar.

## Requirements

| | |
| --- | --- |
| Yeastar PBX | P-Series with the generic SMS API: Appliance 37.23.0.24+, Software 83.23.0.24+, or Cloud Edition |
| NetSIP | Account ID (`SRCxxxxx`) and an API username and password with SMS enabled |
| Network | The PBX must reach the bridge over **HTTPS with a valid certificate**. The bridge must reach the PBX webhook URL and `api.overthewire.com.au` |

The bridge speaks plain HTTP on port 8080, so put a reverse proxy in front of it
(Nginx Proxy Manager, SWAG, Caddy, a Cloudflare tunnel). Only `/yeastar/send` and
`/yeastar/verify` need to be reachable by the PBX.

## Install

### Docker

```
docker run -d --name netsip-sms --restart unless-stopped \
  -e YEASTAR_API_KEY=choose-a-long-random-string \
  -e YEASTAR_SECRET=choose-another-long-random-string \
  -e YEASTAR_WEBHOOK_URL=https://your-pbx.example.com/api/v1.0/webhook/general/xxxx \
  -e NETSIP_ACCOUNT_ID=SRC12345 \
  -e NETSIP_USERNAME=your-api-user \
  -e NETSIP_PASSWORD=your-api-password \
  -e NETSIP_NUMBERS=+61400000001 \
  -v netsip-sms-data:/data -p 8080:8080 \
  sparkybearbomb/netsip-sms-bridge:latest
```

The same image is published as `ghcr.io/benjaminallott/netsip-sms-bridge:latest`.

Prefer a file? Download
[.env.example](https://github.com/BenjaminAllott/netsip-sms-bridge/blob/main/.env.example),
save it as `.env`, fill it in and replace the `-e` lines with `--env-file .env`.

### Unraid

Run once in the Unraid terminal:

```
curl -o /boot/config/plugins/dockerMan/templates-user/my-netsip-sms-bridge.xml \
  https://raw.githubusercontent.com/BenjaminAllott/netsip-sms-bridge/main/unraid/netsip-sms-bridge.xml
```

Then **Docker > Add Container > Template > netsip-sms-bridge**, fill in the fields and
Apply. The container runs as `nobody:users` (99:100), so the appdata folder needs no
extra permissions.

### Docker Compose

```
git clone https://github.com/BenjaminAllott/netsip-sms-bridge.git
cd netsip-sms-bridge
cp .env.example .env      # fill it in
docker compose up -d --build
```

Add `--profile tls` and set `BRIDGE_DOMAIN` to have the bundled Caddy obtain a
certificate and serve HTTPS for you.

## Set up the PBX

**Messaging > Message Channel > Add > SMS**

| Field | Value |
| --- | --- |
| ITSP | General |
| API Key | the value you chose for `YEASTAR_API_KEY` |
| Secret | the value you chose for `YEASTAR_SECRET` |
| API Address for Sending Messages | `https://<bridge host>/yeastar/send` |
| API Address for Verifying Authentication | `https://<bridge host>/yeastar/verify` |
| Webhook URL | copy this into `YEASTAR_WEBHOOK_URL` and restart the bridge |

Under **Number**, add each NetSIP number in E.164 form (`+614...`), choose where its
inbound messages go, and choose which extensions may send from it.

The API key and secret are not issued by anyone: make up two long random strings and
enter the same values on both sides.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `YEASTAR_API_KEY` | required | Key the PBX presents to the bridge |
| `YEASTAR_SECRET` | empty | Signs inbound webhooks to the PBX (recommended) |
| `YEASTAR_WEBHOOK_URL` | required unless inbound is off | Webhook URL shown on the PBX channel |
| `NETSIP_ACCOUNT_ID` | required | Over the Wire account ID |
| `NETSIP_USERNAME` | required | NetSIP API username |
| `NETSIP_PASSWORD` | required | NetSIP API password |
| `NETSIP_NUMBERS` | required | Comma separated numbers this bridge may use. Nothing else on the account is fetched or sent from |
| `INBOUND_MODE` | `poll` | `poll`, `callback` or `off` |
| `POLL_INTERVAL` | `15` | Seconds between inbound checks (minimum 5) |
| `NETSIP_PLUS_PREFIX` | `true` | `true` sends `+61412345678` to NetSIP, `false` sends `61412345678` |
| `DEFAULT_COUNTRY_CODE` | `61` | Expands local numbers such as `0412 345 678` |
| `NETSIP_BASE_URL` | `https://api.overthewire.com.au` | NetSIP API address |
| `PUBLIC_BASE_URL` | empty | Callback mode only: public HTTPS address of the bridge |
| `CALLBACK_TOKEN` | empty | Callback mode only: any long random string |
| `LISTEN_PORT` | `8080` | Port inside the container |
| `STATE_FILE` | `/data/state.json` | Where delivered message IDs are remembered |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO` or `WARNING` |

A blank value is treated as "use the default". The bridge refuses to start if a
required setting is missing and names it in the log.

## How inbound works

- **`poll`** (default): every `POLL_INTERVAL` seconds the bridge lists inbound messages
  on the NetSIP account and forwards the new ones. Delivered IDs are stored in `/data`,
  so nothing is sent twice and messages that arrive while the bridge is down are
  delivered when it comes back. On the very first start, existing messages are marked
  as seen rather than replayed into the PBX.
- **`callback`**: outbound messages ask NetSIP to push replies to
  `PUBLIC_BASE_URL/netsip/inbound`. Per the NetSIP documentation this covers *replies*
  to messages sent through the bridge, not unsolicited inbound messages.
- **`off`**: outbound only.

## Numbers and isolation

`NETSIP_NUMBERS` is an allow-list and the bridge will not start without it.

- **Inbound:** the bridge asks NetSIP only for messages to the listed numbers, then
  checks each result again itself. A message for any other number on the account is
  discarded and never reaches the PBX.
- **Outbound:** the PBX may only send from a listed number. Anything else is refused
  with error 10005 and never reaches NetSIP.

List several numbers to serve them all through one PBX channel; add each of them under
**Number** on that channel.

### Several PBXs on one NetSIP account

Run one container per PBX, each with its own `NETSIP_NUMBERS`, `YEASTAR_API_KEY`,
`YEASTAR_SECRET` and `YEASTAR_WEBHOOK_URL`. Do not list the same number in two
containers, or both PBXs will receive its messages.

Each container still holds the account-wide NetSIP login, because NetSIP has no
per-number credentials. Keep the containers on infrastructure you control; the PBXs
themselves only ever hold their own bridge API key.

## First run checklist

NetSIP's API document does not show what its inbound list or callbacks return, so the
bridge matches field names loosely and logs what it receives.

1. Start the container and check the log shows `listening on :8080`.
2. Send an SMS from Linkus. If NetSIP rejects the number, change `NETSIP_PLUS_PREFIX`.
3. Text your NetSIP number. The first poll logs `first inbound list response`. If
   messages are logged as `unrecognised fields`, add the real field names to the
   `*_KEYS` tuples at the top of `bridge.py`, or open an issue with the log line.
4. If the PBX marks the channel *Unauthorized* although sending works, clear the
   optional "API Address for Verifying Authentication" field.

## Limitations

- Text SMS only: NetSIP does not carry MMS, so MMS from the PBX is refused with error
  10003.
- Delivery receipts from NetSIP are not passed back to the PBX.
- A message to a listed number that is missing from the PBX channel is retried on
  every poll until it ages out of the two day window.

## Endpoints

| Path | Caller | Purpose |
| --- | --- | --- |
| `POST /yeastar/send` | PBX | Send an SMS (Bearer API key) |
| `GET /yeastar/verify` | PBX | Channel connectivity check (Bearer API key) |
| `GET/POST /netsip/inbound` | NetSIP | Reply callback (callback mode, token protected) |
| `GET /healthz` | anyone | Health check |

## Development

```
python -m unittest -v test_bridge
```

The tests run the bridge against in-process mock NetSIP and Yeastar servers. Every push
to `main` runs them, then builds and publishes the image to Docker Hub and GHCR.

Source and issues: https://github.com/BenjaminAllott/netsip-sms-bridge

## Licence

Copyright (C) 2026 Allott Group Pty Ltd.

Released under the
[GNU Affero General Public License v3.0](https://github.com/BenjaminAllott/netsip-sms-bridge/blob/main/LICENSE),
or (at your option) any later version.
You may use and modify it freely, but if you distribute it, or run a modified version
as a service for others, you must make your source available under the same licence.
It comes with no warranty.
