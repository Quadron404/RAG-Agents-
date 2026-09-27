import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { initAppearance } from "./store";
import "./styles/theme.css";

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error("RAG Agents crashed:", error);
    window.dispatchEvent(new CustomEvent("rag:crash", { detail: String(error) }));
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            minHeight: "100dvh",
            display: "grid",
            placeItems: "center",
            background: "#1a110f",
            color: "#ffd9d4",
            font: "14px/1.6 ui-monospace, Menlo, Consolas, monospace",
            padding: 24,
            textAlign: "center",
          }}
        >
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 8 }}>
              RAG Agents hit an unexpected error
            </div>
            <div style={{ opacity: 0.85, whiteSpace: "pre-wrap" }}>{String(this.state.error)}</div>
            <button
              onClick={() => location.reload()}
              style={{
                marginTop: 16,
                padding: "8px 16px",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,.25)",
                background: "rgba(255,255,255,.08)",
                color: "#fff",
                cursor: "pointer",
              }}
            >
              Reload
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

initAppearance();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
);