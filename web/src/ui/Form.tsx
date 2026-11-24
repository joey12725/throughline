import { forwardRef, useId, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react";
import clsx from "clsx";
import { Check } from "lucide-react";
import styles from "./Form.module.css";

export interface FieldProps {
  label: ReactNode;
  htmlFor?: string;
  hint?: ReactNode;
  error?: ReactNode;
  /** lay label and control side by side */
  inline?: boolean;
  children: ReactNode;
  className?: string;
}

/** A labelled form row with hint and error slots. */
export function Field({ label, htmlFor, hint, error, inline, children, className }: FieldProps): ReactNode {
  if (inline) {
    return (
      <div className={clsx(styles.field, styles.inline, className)}>
        <div className={styles.inlineText}>
          <label className={styles.label} htmlFor={htmlFor}>
            {label}
          </label>
          {hint && <span className={styles.hint}>{hint}</span>}
        </div>
        <div className={styles.control}>{children}</div>
        {error && <div className={styles.error}>{error}</div>}
      </div>
    );
  }
  return (
    <div className={clsx(styles.field, className)}>
      <div className={styles.fieldHead}>
        <label className={styles.label} htmlFor={htmlFor}>
          {label}
        </label>
        {hint && <span className={styles.hint}>{hint}</span>}
      </div>
      <div className={styles.control}>{children}</div>
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
  /** render in the mono family (ids, tokens, numbers) */
  mono?: boolean;
  lead?: ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { invalid, mono, lead, className, ...rest },
  ref,
) {
  if (lead) {
    return (
      <div className={clsx(styles.inputWrap, invalid && styles.invalid)}>
        <span className={styles.inputLead} aria-hidden>{lead}</span>
        <input ref={ref} className={clsx(styles.input, styles.bare, mono && styles.mono, className)} {...rest} />
      </div>
    );
  }
  return (
    <input
      ref={ref}
      className={clsx(styles.input, invalid && styles.invalid, mono && styles.mono, className)}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
});

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { invalid, className, children, ...rest },
  ref,
) {
  return (
    <select
      ref={ref}
      className={clsx(styles.select, invalid && styles.invalid, className)}
      aria-invalid={invalid || undefined}
      {...rest}
    >
      {children}
    </select>
  );
});

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
}

/** An accessible on/off switch. */
export function Switch({ checked, onChange, label, disabled, id, className }: SwitchProps): ReactNode {
  const auto = useId();
  const sid = id ?? auto;
  return (
    <button
      id={sid}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={clsx(styles.switch, checked && styles.switchOn, className)}
      onClick={() => onChange(!checked)}
    >
      <span className={styles.knob}>{checked && <Check size={10} strokeWidth={3} />}</span>
    </button>
  );
}
