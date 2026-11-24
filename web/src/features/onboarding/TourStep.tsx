import { type ReactNode } from "react";
import { Settings as SettingsIcon, Layers, BookOpen } from "lucide-react";
import { useWizard } from "./wizard";
import { StepHeader, WizardFooter, Callout } from "./parts";
import { NAV } from "@/app/nav";
import styles from "./Onboarding.module.css";

const BLURBS: Record<string, string> = {
  loom: "Your home — a woven view of what's arriving across every source, newest first.",
  live: "A real-time feed as new messages, events and places land in your hub.",
  day: "Any single day, reconstructed: who you spoke to, where you were, what you did.",
  people: "Everyone you know, each one stitched together from all their handles.",
  places: "Where you've been, on a map and a timeline — from your location history.",
  query: "Ask in SQL (or plain language with an assistant) across your whole archive.",
  sources: "Every connector: health, record counts, and the controls to add more.",
  graph: "How people, places and threads connect — your archive as a constellation.",
};

const TOUR_IDS = ["loom", "live", "day", "people", "places", "query"];

export function TourStep(): ReactNode {
  const wiz = useWizard();

  const cards = TOUR_IDS.map((id) => NAV.find((n) => n.id === id)).filter((n): n is NonNullable<typeof n> => Boolean(n));

  return (
    <div className={styles.step}>
      <StepHeader
        eyebrow="A quick look around"
        title="You're all set — here's the lay of the land"
        lede="Your hub is live, your imports are running, your lenses and pages are keeping themselves up to date. Here's where to find things. You can replay this tour, or re-run setup, from Settings any time."
      />

      <div className={styles.body}>
        <div className={styles.tourGrid}>
          {cards.map((n) => {
            const Icon = n.icon;
            return (
              <div key={n.id} className={styles.tourCard}>
                <span className={styles.tourCardIcon}><Icon size={18} /></span>
                <span className={styles.tourCardTitle}>{n.label}</span>
                <span className={styles.tourCardDesc}>{BLURBS[n.id] ?? n.title}</span>
              </div>
            );
          })}
        </div>

        <div className={styles.tourGrid}>
          <div className={styles.tourCard}>
            <span className={styles.tourCardIcon}><Layers size={18} /></span>
            <span className={styles.tourCardTitle}>Lenses</span>
            <span className={styles.tourCardDesc}>Tables your librarian keeps filled from your conversations — plans, promises, dates — each also a SQL view your agents can read.</span>
          </div>
          <div className={styles.tourCard}>
            <span className={styles.tourCardIcon}><BookOpen size={18} /></span>
            <span className={styles.tourCardTitle}>Pages</span>
            <span className={styles.tourCardDesc}>Living documents that write themselves — a page per day, person and week — so there's always something to read.</span>
          </div>
        </div>

        <div className={styles.tourCard}>
          <span className={styles.tourCardTitle}>Try this first</span>
          <span className={styles.tourCardDesc}>Open <strong>Query</strong> and run one of your saved questions, or ask your connected agent:</span>
          <div className={styles.tryPrompt}>“What did I promise people this week?”</div>
        </div>

        <Callout tone="info" icon={SettingsIcon}>
          Sources, models, lenses, pages, backups and this setup all live under <strong>Settings</strong>. Add a new connector any time from <strong>Sources</strong>.
        </Callout>
      </div>

      <WizardFooter hideSkip continueLabel="Finish & explore" busy={wiz.completing} />
    </div>
  );
}
