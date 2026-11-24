-- Preserve precedence for owner assertions made before people.asserted_by
-- existed, and for owner facts/names/relationships on agent-created people.
UPDATE tl.people p SET asserted_by='owner'
WHERE p.is_owner OR EXISTS (
 SELECT 1 FROM tl.identity_events e WHERE e.asserted_by='owner' AND (
 (e.operation='create' AND e.detail->>'id'=p.id::text)
 OR e.detail->>'person_id'=p.id::text
 OR e.detail->>'from_id'=p.id::text OR e.detail->>'to_id'=p.id::text
 OR e.detail->>'person_a'=p.id::text OR e.detail->>'person_b'=p.id::text
 OR (e.operation='merge' AND (e.detail->>'source'=p.id::text OR e.detail->>'target'=p.id::text))))
 OR EXISTS(SELECT 1 FROM tl.identifiers i WHERE i.person_id=p.id AND i.asserted_by='owner')
 OR EXISTS(SELECT 1 FROM tl.facts f WHERE f.person_id=p.id AND f.asserted_by='owner')
 OR EXISTS(SELECT 1 FROM tl.relations r WHERE (r.from_id=p.id OR r.to_id=p.id) AND r.asserted_by='owner');
