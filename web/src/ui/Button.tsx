import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import clsx from "clsx";
import styles from "./Button.module.css";

export type ButtonVariant = "primary" | "default" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** leading icon node (lucide icon element) */
  icon?: ReactNode;
  /** stretch to the width of the parent */
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "default", size = "md", icon, block, className, children, type, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      className={clsx(styles.btn, styles[variant], styles[size], block && styles.block, className)}
      {...rest}
    >
      {icon && <span className={styles.icon} aria-hidden>{icon}</span>}
      {children != null && <span className={styles.label}>{children}</span>}
    </button>
  );
});

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** required accessible label — icon-only buttons must name themselves */
  label: string;
  size?: ButtonSize;
  variant?: "default" | "ghost";
  /** mark active (e.g. a toggled toolbar button) */
  active?: boolean;
  children: ReactNode;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, size = "md", variant = "ghost", active, className, children, type, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      aria-label={label}
      title={label}
      aria-pressed={active}
      className={clsx(styles.iconBtn, styles[size], styles[variant], active && styles.active, className)}
      {...rest}
    >
      {children}
    </button>
  );
});
