import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: 'var(--canvas)',
        panel: 'var(--panel)',
        ink: 'var(--ink)',
        'ink-2': 'var(--ink-2)',
        line: 'var(--line)',
        action: 'var(--action)',
        'action-ink': 'var(--action-ink)',
        'action-soft': 'var(--action-soft)',
        error: 'var(--error)',
        'error-soft': 'var(--error-soft)',
        warn: 'var(--warn)',
        'warn-soft': 'var(--warn-soft)',
        safe: 'var(--safe)',
        'safe-soft': 'var(--safe-soft)',
        scrim: 'var(--scrim)',
      },
      fontFamily: { sans: ['var(--font-open-sans)', 'system-ui', 'sans-serif'] },
      letterSpacing: { tight: '-0.01em' },
      borderRadius: { pill: '9999px' },
    },
  },
  plugins: [],
};
export default config;
