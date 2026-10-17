# MQTT over TLS

Devices connect to the broker's TLS listener (**8883**, `infra/mosquitto/README.md`); plain 1883 is
for in-cluster services only. Status: the firmware compiles with this configuration; **no TLS
handshake from a real ESP32 has been tested**.

## Broker

Set `MQTT_TLS_CERT_FILE` / `MQTT_TLS_KEY_FILE` (and mount the directory) as described in
`infra/mosquitto/README.md`. For development `sh infra/scripts/gen-dev-certs.sh` creates a local CA
and a 30-day server certificate (output is git-ignored).

## Firmware

* Library: **espMqttClient** (bertmelis), class `espMqttClientSecure`. API used (checked against the
  upstream sources of v1.7.3, not just documentation): `setCACert(const char*)`, `setInsecure()`,
  `setServer(host,port)`, `setCredentials(user,pass)`, `setClientId`, `setKeepAlive`,
  `setCleanSession`, `connect()`, `publish(topic, qos, retain, payload, len)` -> packet id (0 on failure),
  `subscribe(topic, qos)`, `onConnect/onDisconnect/onMessage/onPublish`, `loop()`.
  QoS 1 PUBACK arrives in `onPublish(packetId)`; the firmware removes a buffered reading from the ring
  only then. It is created with `UseInternalTask::NO` so all callbacks run in `loop()` (no locking needed).
* Put the broker's CA (PEM, public) in `config.h` as `MQTT_CA_CERT_PEM`. The dev CA produced by
  `gen-dev-certs.sh` works for a lab; use your real CA in production.
* **Hostname vs certificate**: `MQTT_HOST` must match a name/IP in the broker certificate's SAN.
  Whether the ESP32 TLS stack checks the host name against the certificate for your core version was
  **not verified**; verify negatively in HIL test 1.12 (wrong host must fail).
* **Clock**: certificate validity needs real time. The firmware starts SNTP (`pool.ntp.org`,
  `time.cloudflare.com`) once Wi-Fi is up and **does not attempt MQTT until the clock is valid**
  (>= 2025-01-01). Readings taken before sync are held with uptime stamps and published later with
  reconstructed timestamps flagged `ts_est`. If UDP/123 is blocked on the site network, the device
  never publishes (symptom: Wi-Fi up, no MQTT); point `NTP_SERVER_1` at a local NTP server.
* `ALLOW_INSECURE_DEV`: calls `setInsecure()` (no verification), `#warning` at build time and a loud
  serial banner at every connect. For throw-away lab use only.
* Certificate rotation/expiry of the **CA** requires reflashing (no OTA here). Prefer a long-lived private CA.
* Memory: TLS handshakes need tens of KB of heap; the sketch reports 15 % static RAM use.

## Testing the broker side without hardware

```bash
mosquitto_sub --cafile ca.crt -h <host> -p 8883 -u <user> -P <pass> -t 'aquaguard/<slug>/<unit>/#' -v
openssl s_client -connect <host>:8883 -CAfile ca.crt -tls1_2   # certificate chain / SAN check
```
