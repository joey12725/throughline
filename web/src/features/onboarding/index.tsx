import { useEffect, type ReactNode } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import type { SetupState, StepId } from "@/api/setup";
import { useSetupState } from "@/api/setup";
import { useAuth } from "@/store/auth";
import { RouteFallback } from "@/app/RouteFallback";
import { WizardProvider, useWizard } from "./wizard";
import { WizardFrame } from "./WizardFrame";
import { STEP_IDS, isStepId } from "./steps";
import { WelcomeStep } from "./WelcomeStep";
import { AccountStep } from "./AccountStep";
import { ModelsStep } from "./ModelsStep";
import { PhotosStep } from "./PhotosStep";
import { SourcesStep } from "./SourcesStep";
import { ConnectStep } from "./ConnectStep";
import { CompanionStep } from "./CompanionStep";
import { ImportStep } from "./ImportStep";
import { PeopleStep } from "./PeopleStep";
import { LensesStep } from "./LensesStep";
import { PagesStep } from "./PagesStep";
import { QueriesStep } from "./QueriesStep";
import { AgentStep } from "./AgentStep";
import { TourStep } from "./TourStep";

/** First step that still needs attention — where a reload resumes. */
function resumeStep(state: SetupState | undefined, signedIn: boolean): StepId {
  for (const id of STEP_IDS) {
    if (id === "account" && signedIn) continue;
    const st = state?.steps?.[id]?.status ?? "todo";
    if (st === "todo") return id;
  }
  return "tour";
}

export default function OnboardingWizard(): ReactNode {
  const { step } = useParams<{ step?: string }>();
  const stateQ = useSetupState();
  const signedIn = useAuth((s) => s.status === "signedIn");

  if (!isStepId(step)) {
    if (stateQ.isLoading) return <RouteFallback />;
    return <Navigate to={`/welcome/${resumeStep(stateQ.data, signedIn)}`} replace />;
  }

  return (
    <WizardProvider current={step}>
      <WizardInner current={step} />
    </WizardProvider>
  );
}

function WizardInner({ current }: { current: StepId }): ReactNode {
  const wiz = useWizard();
  const navigate = useNavigate();

  // On step change, scroll the main region to the top and move focus there.
  useEffect(() => {
    const main = document.getElementById("main");
    if (main) {
      main.scrollTo({ top: 0, behavior: "auto" });
      main.focus({ preventScroll: true });
    }
    document.title = "Setup · Throughline";
  }, [current]);

  const onDefer = (): void => {
    try {
      sessionStorage.setItem("tl.setup.deferred", "1");
    } catch {
      /* ignore */
    }
    navigate("/");
  };

  return (
    <WizardFrame current={current} statusOf={wiz.stepStatus} progress={wiz.progress} onPick={wiz.goTo} onDefer={onDefer}>
      <StepView current={current} />
    </WizardFrame>
  );
}

function StepView({ current }: { current: StepId }): ReactNode {
  switch (current) {
    case "welcome": return <WelcomeStep />;
    case "account": return <AccountStep />;
    case "models": return <ModelsStep />;
    case "photos": return <PhotosStep />;
    case "sources": return <SourcesStep />;
    case "connect": return <ConnectStep />;
    case "companion": return <CompanionStep />;
    case "import": return <ImportStep />;
    case "people": return <PeopleStep />;
    case "lenses": return <LensesStep />;
    case "pages": return <PagesStep />;
    case "queries": return <QueriesStep />;
    case "agent": return <AgentStep />;
    case "tour": return <TourStep />;
    default: return <WelcomeStep />;
  }
}
