# Device provisioning

Goal: each ESP32 has its **own** broker account limited by ACL to its four topics, so a stolen
device cannot read other flats or send valve commands (`valve/cmd` is read-only for devices).

## Steps

1. Create the society and flat in the backend (`POST /api/v1/societies`, `/flats`).
2. Issue the device credential. The backend CLI (added by another agent; **present in the repo now**,
   `backend/app/cli.py`):

   ```bash
   cd backend && python -m app.cli provision-sensor --society <slug> --unit <unit>
   ```

   It prints a secret **once** (not stored), the `mosquitto_passwd` command and the ACL block.
   (Check `python -m app.cli provision-sensor --help` for the exact flags; this repo's firmware work
   did not run it against a database.)
   Alternative used by the compose stack (`make provision-sensor`): `MQTT_DEVICES` + `MQTT_DEVICE_SECRET` in `infra/mosquitto`,
   password from `sh infra/mosquitto/device_password.sh <slug>/<unit>`.
3. Put the values in `edge/firmware/aquaguard_sensor/config.h` (git-ignored; copy from
   `config.h.example`): `MQTT_USER`, `MQTT_PASSWORD`, `SOCIETY_SLUG`, `FLAT_UNIT`, `MQTT_CA_CERT_PEM`.
   The client id is derived as `aquaguard-<slug>-<unit>`.
4. Build and flash from a trusted machine. Never commit `config.h`; never paste secrets in issues or chat.
5. Verify with a privileged subscriber (`mosquitto_sub`): readings appear on `aquaguard/<slug>/<unit>/flow`.
   Mosquitto drops unauthorised publishes silently, so absence of data can mean an ACL mismatch.

## Known inconsistency to resolve (backend/infra owners)

* `python -m app.cli provision-sensor` names the device user `<slug>.<unit>` (dot) and writes ACL
  lines for that user.
* `infra/mosquitto` (entrypoint, `device_password.sh`, ACL template) and the simulator use
  `<slug>/<unit>` (slash).

The firmware uses whatever `MQTT_USER` you configure; the two sides of **your** broker must agree.
Pick one scheme per deployment and do not mix.

## Hardening notes

* Fail closed: the firmware refuses to connect if the user/password are empty or still `CHANGE_ME`,
  or if the CA is the placeholder (unless built with `ALLOW_INSECURE_DEV`).
* Credentials live in flash in clear text (config.h compiled in). Enable ESP32 flash encryption and
  secure boot for deployed units (not configured here). Rotating a password means reflashing.
* NVS provisioning (credentials written at the factory instead of compiled in) is **not implemented**.
