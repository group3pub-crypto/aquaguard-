// Interface STUB for the optional pressure (psi) and temperature (tmp) sensors.
// The sensor models, wiring and protocol are UNSPECIFIED, so NO driver is provided or invented.
// Until an integrator supplies one, the payload carries "psi":null,"tmp":null (schema-valid).
//
// To add a sensor: implement OptionalSensor for your hardware, add it to a new
// aquaguard_<sensor>.cpp, extend aqg::Reading/formatFlowJson (core) with the value, add host tests,
// and only then define ENABLE_PSI_SENSOR / ENABLE_TMP_SENSOR. Units: psi 0..500, tmp -50..150 degC
// (backend FlowPayload bounds).
#pragma once

#if defined(ENABLE_PSI_SENSOR) || defined(ENABLE_TMP_SENSOR)
#error "ENABLE_PSI_SENSOR / ENABLE_TMP_SENSOR set but no driver exists in this repo. See optional_sensors.h."
#endif

class OptionalSensor {
 public:
  virtual ~OptionalSensor() {}
  virtual bool begin() = 0;
  // true + value on a plausible reading; false => report null and raise a fault code.
  virtual bool read(float *value) = 0;
};
