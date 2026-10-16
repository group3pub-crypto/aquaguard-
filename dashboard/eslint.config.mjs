import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: ['.next/**', 'node_modules/**', 'next-env.d.ts', 'coverage/**'] },
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-non-null-assertion': 'error',
      // eslint-plugin-react-hooks v7 (React Compiler rules) flag the "latest ref" and
      // "reset state when key changes" patterns used deliberately in hooks/ and context/
      // (covered by tests). Kept off rather than rewriting working, tested hooks.
      'react-hooks/refs': 'off',
      'react-hooks/set-state-in-effect': 'off',
      'no-console': ['error', { allow: ['warn', 'error'] }],
    },
  },
];

export default config;
