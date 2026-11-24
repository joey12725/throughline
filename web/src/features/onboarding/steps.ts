/*
 * The onboarding step model — the single source of truth for the stepper, the
 * deep-link segment (/ui/welcome/<step>) and the order of the flow. StepId comes
 * from the setup contract so the UI and the server agree on every name.
 */
import type { StepId } from "@/api/setup";

export interface StepMeta {
  id: StepId;
  /** short label in the stepper */
  label: string;
  /** the eyebrow shown above the headline */
  eyebrow: string;
}

export const STEPS: StepMeta[] = [
  { id: "welcome", label: "Welcome", eyebrow: "Getting started" },
  { id: "account", label: "Account", eyebrow: "Your owner account" },
  { id: "models", label: "Intelligence", eyebrow: "Search & assistant" },
  { id: "photos", label: "Photos", eyebrow: "Your image library" },
  { id: "sources", label: "Sources", eyebrow: "What you use" },
  { id: "connect", label: "Connect", eyebrow: "Set up your sources" },
  { id: "companion", label: "Companion", eyebrow: "Your desktop app" },
  { id: "import", label: "Import", eyebrow: "Backfill your history" },
  { id: "people", label: "People", eyebrow: "Who's who" },
  { id: "lenses", label: "Lenses", eyebrow: "Your first lenses" },
  { id: "pages", label: "Pages", eyebrow: "Pages that write themselves" },
  { id: "queries", label: "Queries", eyebrow: "Ask your first questions" },
  { id: "agent", label: "Agent", eyebrow: "Connect your agent" },
  { id: "tour", label: "Tour", eyebrow: "A quick look around" },
];

export const STEP_IDS: StepId[] = STEPS.map((s) => s.id);

export function stepMeta(id: StepId): StepMeta {
  return STEPS.find((s) => s.id === id) ?? STEPS[0]!;
}

export function stepIndex(id: StepId): number {
  return STEP_IDS.indexOf(id);
}

export function isStepId(x: string | undefined | null): x is StepId {
  return !!x && (STEP_IDS as string[]).includes(x);
}

export function nextStepId(id: StepId): StepId | null {
  const i = stepIndex(id);
  return i >= 0 && i < STEP_IDS.length - 1 ? STEP_IDS[i + 1]! : null;
}

export function prevStepId(id: StepId): StepId | null {
  const i = stepIndex(id);
  return i > 0 ? STEP_IDS[i - 1]! : null;
}
