import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from './ErrorBoundary';

function Bomb({ armed }: { armed: boolean }) {
  if (armed) throw new Error('kaboom');
  return <p>all good</p>;
}

describe('ErrorBoundary', () => {
  it('shows a fallback and recovers on retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    function Harness() {
      const [armed, setArmed] = useState(true);
      return (
        <>
          <button onClick={() => setArmed(false)}>defuse</button>
          <ErrorBoundary>
            <Bomb armed={armed} />
          </ErrorBoundary>
        </>
      );
    }
    render(<Harness />);
    expect(screen.getByRole('alert')).toHaveTextContent('kaboom');
    await userEvent.click(screen.getByText('defuse'));
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.getByText('all good')).toBeInTheDocument();
  });
});
