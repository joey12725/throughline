/*
 * The JSON tab: the raw rows as the server returned them (capped to a safe slice so
 * a huge result does not freeze the pane). This is the literal payload, for copying
 * into a script or checking an exact value.
 */
import { useMemo, type ReactNode } from "react";
import type { SqlResult } from "@/api/types";
import styles from "./query.module.css";

const CAP = 500;

export function ResultJson({ result }: { result: SqlResult }): ReactNode {
  const text = useMemo(() => {
    const slice = result.rows.slice(0, CAP);
    return JSON.stringify(slice, null, 2);
  }, [result.rows]);

  return (
    <div className={styles.jsonTab}>
      {result.rows.length > CAP && (
        <div className={styles.jsonNote}>Showing the first {CAP.toLocaleString("en-US")} of {result.rowCount.toLocaleString("en-US")} rows.</div>
      )}
      <pre className={styles.jsonRaw}>{text}</pre>
    </div>
  );
}
