/*
 * Wizard context: the shared state and navigation every step reads. Progress is
 * persisted to the server (useSaveSetupState) so a reload resumes on the right
 * step, and the URL carries the current step (/ui/welcome/<step>) for deep links.
 */
import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import {
  useSetupState,
  useSaveSetupState,
  useCompleteSetup,
  type SetupState,
  type StepId,
  type StepStatus,
} from "@/api/setup";
import { useAuth } from "@/store/auth";
import { STEPS, nextStepId, prevStepId } from "./steps";

export interface WizardCtx {
  state: SetupState | undefined;
  loading: boolean;
  current: StepId;
  isSignedIn: boolean;
  mode: SetupState["mode"] | "fresh";
  choices: SetupState["choices"];
  selectedSources: string[];
  /** resolved status for the stepper (account is implicitly done once signed in) */
  stepStatus: (id: StepId) => StepStatus;
  progress: number;
  goTo: (id: StepId) => void;
  next: () => void;
  skip: () => void;
  back: () => void;
  markStatus: (id: StepId, status: StepStatus) => void;
  setMode: (mode: SetupState["mode"]) => void;
  setChoices: (patch: Partial<SetupState["choices"]>) => void;
  finish: () => void;
  saving: boolean;
  completing: boolean;
}

const Ctx = createContext<WizardCtx | null>(null);

export function useWizard(): WizardCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useWizard outside WizardProvider");
  return ctx;
}

export function WizardProvider({ current, children }: { current: StepId; children: ReactNode }): ReactNode {
  const navigate = useNavigate();
  const stateQ = useSetupState();
  const saveState = useSaveSetupState();
  const complete = useCompleteSetup();
  const isSignedIn = useAuth((s) => s.status === "signedIn");

  const state = stateQ.data;
  const choices = useMemo(() => state?.choices ?? {}, [state?.choices]);
  const selectedSources = useMemo<string[]>(
    () => (Array.isArray(choices.sources) ? (choices.sources as string[]) : []),
    [choices.sources],
  );

  const stepStatus = useCallback(
    (id: StepId): StepStatus => {
      if (id === "account" && isSignedIn && state?.steps?.account?.status !== "skipped") return "done";
      return state?.steps?.[id]?.status ?? "todo";
    },
    [state?.steps, isSignedIn],
  );

  const progress = useMemo(() => {
    const settled = STEPS.filter((s) => {
      const st = stepStatus(s.id);
      return st === "done" || st === "skipped";
    }).length;
    return settled / STEPS.length;
  }, [stepStatus]);

  const goTo = useCallback(
    (id: StepId) => {
      navigate(`/welcome/${id}`);
    },
    [navigate],
  );

  const markStatus = useCallback(
    (id: StepId, status: StepStatus) => {
      const prev = state?.steps ?? {};
      saveState.mutate({ steps: { ...prev, [id]: { status, at: new Date().toISOString() } } });
    },
    [saveState, state?.steps],
  );

  const setMode = useCallback(
    (mode: SetupState["mode"]) => {
      saveState.mutate({ mode });
    },
    [saveState],
  );

  const setChoices = useCallback(
    (patch: Partial<SetupState["choices"]>) => {
      saveState.mutate({ choices: { ...choices, ...patch } });
    },
    [saveState, choices],
  );

  const finish = useCallback(() => {
    complete.mutate(undefined, { onSettled: () => navigate("/") });
  }, [complete, navigate]);

  const next = useCallback(() => {
    markStatus(current, "done");
    const n = nextStepId(current);
    if (n) goTo(n);
    else finish();
  }, [current, markStatus, goTo, finish]);

  const skip = useCallback(() => {
    markStatus(current, "skipped");
    const n = nextStepId(current);
    if (n) goTo(n);
    else finish();
  }, [current, markStatus, goTo, finish]);

  const back = useCallback(() => {
    const p = prevStepId(current);
    if (p) goTo(p);
  }, [current, goTo]);

  const value = useMemo<WizardCtx>(
    () => ({
      state,
      loading: stateQ.isLoading,
      current,
      isSignedIn,
      mode: state?.mode ?? "fresh",
      choices,
      selectedSources,
      stepStatus,
      progress,
      goTo,
      next,
      skip,
      back,
      markStatus,
      setMode,
      setChoices,
      finish,
      saving: saveState.isPending,
      completing: complete.isPending,
    }),
    [state, stateQ.isLoading, current, isSignedIn, choices, selectedSources, stepStatus, progress, goTo, next, skip, back, markStatus, setMode, setChoices, finish, saveState.isPending, complete.isPending],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
