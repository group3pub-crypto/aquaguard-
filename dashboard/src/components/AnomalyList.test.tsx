import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AnomalyList } from './AnomalyList';
import { anomaly, ruleAnomaly } from '@/test/fixtures';
import type { Anomaly } from '@/lib/types';

describe('AnomalyList', () => {
  it('renders SHAP top_factors labelled by generated_by, with percentages', () => {
    render(<AnomalyList anomalies={[anomaly]} />);
    expect(screen.getByText(/SHAP values from the ML classifier/)).toBeInTheDocument();
    expect(screen.getByText(/SHAP \+0\.310/)).toBeInTheDocument();
    expect(screen.getByText(/Severity 62%/)).toBeInTheDocument();
    expect(screen.getByText(/Confidence 87%/)).toBeInTheDocument();
  });

  it('renders rule-heuristic factors honestly as non-ML', () => {
    render(<AnomalyList anomalies={[ruleAnomaly]} />);
    expect(screen.getByText(/rule-based heuristic \(not ML\)/)).toBeInTheDocument();
    expect(screen.getByText(/constant night flow/)).toBeInTheDocument();
  });

  it.each<[string, Anomaly['explanation']]>([
    ['null', null],
    ['empty object', {}],
    ['top_factors null', { generated_by: 'rule_heuristic', top_factors: null }],
    ['legacy reasons key', { reasons: [{ feature: 'x', text: 'y' }] } as unknown as Anomaly['explanation']],
    ['garbage factors', { generated_by: 'x', top_factors: [null, 3] as unknown as [] }],
  ])('does not crash when explanation is %s', (_n, explanation) => {
    expect(() => render(<AnomalyList anomalies={[{ ...anomaly, explanation, est_loss_lpm: null }]} />)).not.toThrow();
  });
});
