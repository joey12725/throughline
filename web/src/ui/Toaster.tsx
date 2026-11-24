import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import clsx from "clsx";
import { Check, Info, TriangleAlert, CircleAlert, X } from "lucide-react";
import styles from "./Toaster.module.css";
import { useToasts, type Toast, type ToastTone } from "./toast";
import { IconButton } from "./Button";

const ICON: Record<ToastTone, typeof Info> = {
  neutral: Info,
  ok: Check,
  warn: TriangleAlert,
  bad: CircleAlert,
};

function ToastView({ t }: { t: Toast }): ReactNode {
  const dismiss = useToasts((s) => s.dismiss);
  useEffect(() => {
    if (t.duration <= 0) return;
    const timer = window.setTimeout(() => dismiss(t.id), t.duration);
    return () => window.clearTimeout(timer);
  }, [t.id, t.duration, dismiss]);
  const Icon = ICON[t.tone];
  return (
    <div className={clsx(styles.toast, styles[t.tone])} role="status">
      <span className={styles.icon} aria-hidden>
        <Icon size={16} />
      </span>
      <div className={styles.text}>
        <div className={styles.title}>{t.title}</div>
        {t.body && <div className={styles.body}>{t.body}</div>}
        {t.action && (
          <button type="button" className={styles.action} onClick={() => { t.action?.onClick(); dismiss(t.id); }}>
            {t.action.label}
          </button>
        )}
      </div>
      <IconButton label="Dismiss" size="sm" onClick={() => dismiss(t.id)}>
        <X />
      </IconButton>
    </div>
  );
}

/** Mount once near the app root. */
export function Toaster(): ReactNode {
  const toasts = useToasts((s) => s.toasts);
  return createPortal(
    <div className={styles.region} role="region" aria-label="Notifications">
      {toasts.map((t) => (
        <ToastView key={t.id} t={t} />
      ))}
    </div>,
    document.body,
  );
}
