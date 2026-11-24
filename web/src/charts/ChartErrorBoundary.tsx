import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState } from "@/ui";

interface Props {
  children: ReactNode;
  lens: string;
}
interface State {
  error: Error | null;
}

/** A renderer that throws must not take the whole screen down. */
export class ChartErrorBoundary extends Component<Props, State> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State {
    return { error };
  }
  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error(`[charts] ${this.props.lens} failed to render`, error, info.componentStack);
  }
  componentDidUpdate(prev: Props): void {
    if (prev.children !== this.props.children && this.state.error) this.setState({ error: null });
  }
  render(): ReactNode {
    if (this.state.error) {
      return <ErrorState title="This chart could not be drawn" error={this.state.error} />;
    }
    return this.props.children;
  }
}
