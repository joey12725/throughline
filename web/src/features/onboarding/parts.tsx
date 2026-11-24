/*
 * Small shared pieces of the wizard chrome: the step header (eyebrow + display
 * headline + lede), the sticky footer (Back / Skip / Continue with Enter), and a
 * callout box. Keeping them here keeps every step visually identical.
 */
import { useEffect, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, Info, TriangleAlert, type LucideIcon } from "lucide-react";
import { Button } from "@/ui";
import { useWizard } from "./wizard";
import styles from "./Onboarding.module.css";

export function StepHeader({
  eyebrow,
  title,
  lede,
}: {
  eyebrow: ReactNode;
  title: ReactNode;
  lede?: ReactNode;
}): ReactNode {
  return (
    <header className={styles.stepHeader}>
      <div className={styles.eyebrow}>{eyebrow}</div>
      <h1 className={styles.headline}>{title}</h1>
      {lede && <p className={styles.lede}>{lede}</p>}
    </header>
  );
}

export interface WizardFooterProps {
  /** overrides the default continue (mark current done → next) */
  onContinue?: () => void;
  continueLabel?: string;
  continueDisabled?: boolean;
  busy?: boolean;
  hideSkip?: boolean;
  hideBack?: boolean;
  hideContinue?: boolean;
  skipLabel?: string;
  /** extra controls placed left of Skip */
  extra?: ReactNode;
}

/** The sticky wizard footer. Enter activates Continue from anywhere safe. */
export function WizardFooter({
  onContinue,
  continueLabel = "Continue",
  continueDisabled = false,
  busy = false,
  hideSkip = false,
  hideBack = false,
  hideContinue = false,
  skipLabel = "Skip",
  extra,
}: WizardFooterProps): ReactNode {
  const wiz = useWizard();
  const go = onContinue ?? wiz.next;
  const canBack = !hideBack && wiz.current !== "welcome";

  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key !== "Enter" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === "TEXTAREA" || tag === "BUTTON" || tag === "A" || tag === "SELECT") return;
      if (t?.isContentEditable) return;
      // A modal dialog is open — let it own Enter.
      if (document.querySelector("dialog[open]")) return;
      if (hideContinue || continueDisabled || busy) return;
      e.preventDefault();
      go();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, continueDisabled, busy, hideContinue]);

  return (
    <footer className={styles.footer}>
      {canBack && (
        <Button variant="ghost" icon={<ArrowLeft size={15} />} onClick={wiz.back} disabled={busy}>
          Back
        </Button>
      )}
      <span className={styles.footerSpacer} />
      {extra}
      {!hideSkip && (
        <Button variant="ghost" onClick={wiz.skip} disabled={busy}>
          {skipLabel}
        </Button>
      )}
      {!hideContinue && (
        <Button
          variant="primary"
          icon={<ArrowRight size={15} />}
          onClick={go}
          disabled={continueDisabled || busy}
        >
          {busy ? "Working…" : continueLabel}
        </Button>
      )}
    </footer>
  );
}

export function Callout({
  tone = "neutral",
  icon,
  children,
}: {
  tone?: "neutral" | "info" | "warn";
  icon?: LucideIcon;
  children: ReactNode;
}): ReactNode {
  const Icon = icon ?? (tone === "warn" ? TriangleAlert : Info);
  return (
    <div className={styles.callout} data-tone={tone}>
      <Icon size={16} className={styles.calloutIcon} aria-hidden />
      <div className={styles.calloutBody}>{children}</div>
    </div>
  );
}
