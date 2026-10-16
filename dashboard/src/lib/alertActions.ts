// Mirrors backend services/alert_engine.py TRANSITIONS so the UI only offers actions the
// backend will accept (anything else would answer 409). The backend remains the authority.
const T = {
  acknowledge: ['nudge', 'alert', 'ticket', 'auto_shutoff', 'snoozed'],
  snooze: ['nudge', 'alert', 'ticket', 'acknowledged', 'snoozed'],
  override: ['nudge', 'alert', 'ticket', 'auto_shutoff', 'acknowledged', 'snoozed'],
  resolve: ['nudge', 'alert', 'ticket', 'auto_shutoff', 'acknowledged', 'snoozed'],
} as const;

export type AlertAction = keyof typeof T;

export function canTransition(action: AlertAction, state: string): boolean {
  return (T[action] as readonly string[]).includes(state);
}
