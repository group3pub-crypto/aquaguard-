'use client';
import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { ErrorState } from './States';

interface Props {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('UI error:', error.message, info.componentStack);
  }

  reset = (): void => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return (
      <main className="mx-auto max-w-xl p-6">
        <ErrorState title="Something went wrong" message={error.message} onRetry={this.reset} />
      </main>
    );
  }
}
