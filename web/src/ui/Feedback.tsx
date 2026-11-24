import type { ReactNode } from "react";
import clsx from "clsx";
import { TriangleAlert } from "lucide-react";
import styles from "./Feedback.module.css";
import { ApiError } from "@/api/errors";
import { Button } from "./Button";

/** A shimmering placeholder block. `lines` stacks several rows. */
export function Skeleton({
  width,
  height = 14,
  lines = 1,
  radius,
  className,
}: {
  width?: number | string;
  height?: number | string;
  lines?: number;
  radius?: number | string;
  className?: string;
}): ReactNode {
  if (lines > 1) {
    return (
      <div className={clsx(styles.stack, className)} aria-hidden>
        {Array.from({ length: lines }).map((_, i) => (
          <span
            key={i}
            className={styles.skeleton}
            style={{ width: i === lines - 1 ? "70%" : (width ?? "100%"), height, borderRadius: radius }}
          />
        ))}
      </div>
    );
  }
  return (
    <span
      className={clsx(styles.skeleton, className)}
      style={{ width: width ?? "100%", height, borderRadius: radius }}
      aria-hidden
    />
  );
}

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
  /** compact variant for inside a panel */
  compact?: boolean;
}

/** A designed "nothing here" state — every empty surface has one (design.md). */
export function EmptyState({ icon, title, description, action, className, compact }: EmptyStateProps): ReactNode {
  return (
    <div className={clsx(styles.empty, compact && styles.compact, className)} role="status">
      {icon && <div className={styles.emptyIcon} aria-hidden>{icon}</div>}
      <div className={styles.emptyTitle}>{title}</div>
      {description && <div className={styles.emptyDesc}>{description}</div>}
      {action && <div className={styles.emptyAction}>{action}</div>}
    </div>
  );
}

export interface ErrorStateProps {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}

/** Shows the server's own detail and SQLSTATE so an error is honest, not generic. */
export function ErrorState({ error, title, onRetry, className }: ErrorStateProps): ReactNode {
  const api = error instanceof ApiError ? error : null;
  const message = api ? api.message : error instanceof Error ? error.message : String(error);
  return (
    <div className={clsx(styles.error, className)} role="alert">
      <div className={styles.errorIcon} aria-hidden>
        <TriangleAlert size={18} />
      </div>
      <div className={styles.errorBody}>
        <div className={styles.errorTitle}>{title ?? "Something went wrong"}</div>
        <div className={styles.errorMsg}>{message}</div>
        <div className={styles.errorMeta}>
          {api?.status ? <span className="tnum">HTTP {api.status}</span> : null}
          {api?.code ? <span className="tnum">SQLSTATE {api.code}</span> : null}
        </div>
        {api?.issues && api.issues.length > 0 && (
          <ul className={styles.issues}>
            {api.issues.map((iss, i) => (
              <li key={i}>
                <code>{iss.loc.filter((p) => p !== "body").join(".")}</code> {iss.msg}
              </li>
            ))}
          </ul>
        )}
        {onRetry && (
          <div className={styles.errorAction}>
            <Button size="sm" onClick={onRetry}>
              Try again
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
