/*
 * People: the constellation (/ui/people) and the person read model
 * (/ui/people/:id). One screen file, dispatched on the route param.
 */
import type { ReactNode } from "react";
import { useParams } from "react-router-dom";
import { Constellation } from "./Constellation";
import { PersonPage } from "./PersonPage";

export default function PeopleScreen(): ReactNode {
  const { id } = useParams();
  const personId = id != null ? Number(id) : NaN;
  if (id != null && Number.isFinite(personId)) {
    return <PersonPage key={personId} id={personId} />;
  }
  return <Constellation />;
}
