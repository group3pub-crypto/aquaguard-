# Platform notes: "Wokio" and Wokwi

## Wokio

The request mentioned a platform called **"Wokio"**. The search carried out for this task found **no
product by that name**. Nothing in this repository integrates with it and **no Wokio endpoint,
SDK, API or feature has been invented or assumed**. If you have a specific vendor, URL or product
documentation for "Wokio", supply it and the integration can be assessed on facts.

## Wokwi (closest real product - optional, simulation only)

The closest real match is **Wokwi**, an online simulator for microcontroller projects including ESP32.
The following is what the task brief reported; **it was not re-verified in this session** (the
docs site was not reachable from the sandbox), so check the current Wokwi documentation before relying on it:

* Wi-Fi in an ESP32 simulation goes through a simulated access point and a *public gateway* (traffic
  is monitored, no access to your local network) or a paid *private gateway*.
* `wokwi-cli` for CI needs an API token.
* It is a **simulator of firmware and virtual parts**. It is *not* an IoT cloud, dashboard, alerting,
  or workflow platform.

What that means here:

* The dashboard, alerts and valve workflow are **AquaGuard's own** backend (FastAPI + alert state
  machine) and dashboard / Telegram bot. Nothing depends on Wokwi or on any third-party IoT cloud.
* Wokwi *could* be used to run the sketch without a board. Whether this sketch (espMqttClient with TLS,
  NVS, the interrupt on GPIO27 with a simulated pulse source) runs there **has not been tried**, and
  the public gateway would need a broker reachable from the internet; do **not** point a simulated
  device at a production broker or real credentials. No `wokwi.toml` / `diagram.json` is provided
  because their exact format was not verified here.
* The repository's own Python simulator (`python -m edge.simulator`) plus the host test harness
  (`make -C edge/firmware test`) cover broker-level and logic-level testing without any third-party service.
