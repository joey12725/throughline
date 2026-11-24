import { type ReactNode } from "react";
import { useRouteError, useNavigate } from "react-router-dom";
import { ErrorState, Button } from "@/ui";
import styles from "./RouteError.module.css";

/** Per-route error boundary. Shows the server's own detail, not a generic page. */
export function RouteError(): ReactNode {
  const error = useRouteError();
  const navigate = useNavigate();
  return (
    <div className={styles.wrap}>
      <ErrorState error={error} title="This screen hit an error" onRetry={() => navigate(0)} />
      <div className={styles.actions}>
        <Button onClick={() => navigate("/")}>Back to Loom</Button>
      </div>
    </div>
  );
}
