/**
 * React Error Boundary — catches render errors in child components and
 * displays a graceful fallback instead of an unhandled React invariant.
 */
import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("React Error Boundary caught:", error, errorInfo.componentStack);
  }

  render() {
    if (this.state.hasError) {
      const fallback = this.props.fallback ?? (
        <div style={{ padding: 24, color: "var(--text-1)", textAlign: "center" }}>
          <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
            RAG Agents hit an unexpected error
          </div>
          <div style={{ fontSize: 13, opacity: 0.7, marginBottom: 16 }}>
            {this.state.error?.message || "Unknown error"}
          </div>
          <button
            onClick={() => {
              this.setState({ hasError: false, error: null });
              window.location.reload();
            }}
            style={{
              padding: "8px 16px",
              borderRadius: 8,
              border: "1px solid var(--line)",
              background: "var(--bg2)",
              color: "var(--text-1)",
              cursor: "pointer",
              fontSize: 13,
            }}
          >
            Reload
          </button>
        </div>
      );
      return <div>{fallback}</div>;
    }
    return this.props.children;
  }
}

/** Wraps children so that a render error in one view doesn't kill the whole app. */
export function withErrorBoundary(children: ReactNode, fallback?: ReactNode): ReactNode {
  return <ErrorBoundary fallback={fallback}>{children}</ErrorBoundary>;
}
