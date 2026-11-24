import { type ReactNode } from "react";
import { AlertCircle } from "lucide-react";
import { setupProblem, type SetupProblem } from "@/api/setup";
import styles from "./Onboarding.module.css";

/**
 * Render a SetupProblem the way the contract intends: a title, a human sentence,
 * what to do next (hint), and the per-item `problems` list when present — so a
 * failure says *why* and *what next*, never just a status code.
 */
export function ProblemView({ problem }: { problem: SetupProblem }): ReactNode {
  return (
    <div className={styles.problem} role="alert">
      <div className={styles.problemTitle}>
        <AlertCircle size={16} aria-hidden />
        {problem.title}
      </div>
      <p className={styles.problemMsg}>{problem.message}</p>
      {problem.hint && <p className={styles.problemHint}>{problem.hint}</p>}
      {problem.problems && problem.problems.length > 0 && (
        <ul className={styles.problemList}>
          {problem.problems.map((p, i) => (
            <li key={i}>
              <code>{p.where}</code> — {p.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Convenience: render any thrown error through the SetupProblem shape. */
export function ErrorProblem({ error }: { error: unknown }): ReactNode {
  return <ProblemView problem={setupProblem(error)} />;
}
