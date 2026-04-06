import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error("admin UI crash:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-6">
          <div className="max-w-lg w-full bg-slate-900 border border-rose-700 rounded-lg p-6 space-y-3">
            <h1 className="text-xl font-semibold text-rose-300">Что-то сломалось</h1>
            <p className="text-sm text-slate-300">
              Админка упала на клиенте. Подробности в консоли браузера.
            </p>
            <pre className="text-xs text-slate-400 overflow-auto max-h-48 bg-slate-950 p-2 rounded">
              {this.state.error.message}
            </pre>
            <button
              onClick={() => window.location.reload()}
              className="px-3 py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-white text-sm"
            >
              Перезагрузить
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
