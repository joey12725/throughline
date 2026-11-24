/*
 * A minimal line diff for showing the helper's proposed statement against the
 * one in the editor. LCS over lines; good enough for a side-by-side of two SQL
 * statements. Pure and unit-tested.
 */
export type DiffOp = "same" | "add" | "del";

export interface DiffLine {
  op: DiffOp;
  text: string;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = before.length ? before.split("\n") : [];
  const b = after.length ? after.split("\n") : [];
  const n = a.length;
  const m = b.length;

  // LCS length table.
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] = a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ op: "same", text: a[i] as string });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      out.push({ op: "del", text: a[i] as string });
      i++;
    } else {
      out.push({ op: "add", text: b[j] as string });
      j++;
    }
  }
  while (i < n) out.push({ op: "del", text: a[i++] as string });
  while (j < m) out.push({ op: "add", text: b[j++] as string });
  return out;
}

/** Whether the two statements differ at all (ignoring trailing whitespace). */
export function statementsDiffer(before: string, after: string): boolean {
  return before.trimEnd() !== after.trimEnd();
}

export interface DiffStat {
  added: number;
  removed: number;
}

export function diffStat(lines: DiffLine[]): DiffStat {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.op === "add") added++;
    else if (l.op === "del") removed++;
  }
  return { added, removed };
}
