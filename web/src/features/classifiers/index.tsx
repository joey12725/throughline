/*
 * Classifiers (/ui/classifiers): the list, the editor (/new, /:name/edit), one
 * classifier (/:name) and the review of one judged conversation
 * (/:name/threads/:threadId). One lazy chunk; the route decides the screen.
 * The classifier routes are owner-only, so other tokens get a note.
 */
import type { ReactNode } from "react";
import { useLocation, useParams } from "react-router-dom";
import { Lock } from "lucide-react";
import { EmptyState, Page } from "@/ui";
import { useBootstrap } from "@/api";
import { ClassifierList } from "./List";
import { ClassifierEditor } from "./Editor";
import { ClassifierDetailPage } from "./Detail";
import { ThreadReview } from "./ThreadReview";

export default function ClassifiersScreen(): ReactNode {
  const { name, threadId } = useParams();
  const { pathname } = useLocation();
  const { data: boot } = useBootstrap();

  if (boot && boot.token.kind !== "owner") {
    return (
      <Page width="readable">
        <EmptyState icon={<Lock size={24} />} title="Owner tokens only" description="Classifiers are defined and reviewed by the owner." />
      </Page>
    );
  }
  if (pathname.endsWith("/classifiers/new")) return <ClassifierEditor />;
  if (name && threadId) return <ThreadReview key={`${name}:${threadId}`} name={name} threadId={threadId} />;
  if (name && pathname.endsWith("/edit")) return <ClassifierEditor key={`edit:${name}`} name={name} />;
  if (name) return <ClassifierDetailPage key={name} name={name} />;
  return <ClassifierList />;
}
