import { getConfig } from './config';

// Self-registration is OFF on the backend unless ALLOW_SELF_REGISTRATION=true (then
// POST /auth/register answers 404). We cannot probe it without side effects, so the link is
// driven by the runtime flag and additionally hidden for the rest of the page session once a
// 404 has been observed.
let registrationRefused = false;

export function markRegistrationDisabled(): void {
  registrationRefused = true;
}
export function resetRegistrationProbe(): void {
  registrationRefused = false;
}
export function registrationAvailable(): boolean {
  return getConfig().allowSelfRegistration && !registrationRefused;
}
