import { useState, type ReactNode } from "react";
import clsx from "clsx";
import { Copy, Check } from "lucide-react";
import styles from "./CodeBlock.module.css";
import { IconButton } from "./Button";

export interface CodeBlockProps {
  code: string;
  /** a language tag shown in the corner (purely decorative) */
  lang?: string;
  /** render inline (no copy button, no block padding) */
  inline?: boolean;
  /** wrap long lines instead of scrolling */
  wrap?: boolean;
  className?: string;
}

/** Monospaced code with a copy affordance. Record text is never passed here. */
export function CodeBlock({ code, lang, inline, wrap, className }: CodeBlockProps): ReactNode {
  const [copied, setCopied] = useState(false);
  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked */
    }
  };

  if (inline) {
    return <code className={clsx(styles.inline, className)}>{code}</code>;
  }

  return (
    <div className={clsx(styles.block, className)}>
      <div className={styles.toolbar}>
        {lang && <span className={styles.lang}>{lang}</span>}
        <IconButton label={copied ? "Copied" : "Copy"} size="sm" onClick={copy}>
          {copied ? <Check /> : <Copy />}
        </IconButton>
      </div>
      <pre className={clsx(styles.pre, wrap && styles.wrap)}>
        <code>{code}</code>
      </pre>
    </div>
  );
}
