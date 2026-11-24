-- Pages: GBrain mechanics, ported natively (docs/v11/pages.md).
--
-- * Compiled truth + timeline: a page is a rewriteable top (the current
--   picture) and an append-only '## Timeline' of dated lines. The lines are
--   rows here (tl.page_timeline, deduplicated like GBrain's idx_timeline_dedup)
--   and are re-rendered into every revision, so a write can never drop them.
--   tl.pages.truth_updated_at says when the top last changed (stale truth:
--   the timeline is newer than the top).
-- * Page kinds registry (GBrain schema packs, simplified): description,
--   recommended sections, recency half-life for search. A kind a lens invents
--   is recorded as a 'candidate'.
-- * Slug redirects for merged pages (GBrain slug_aliases).
-- * Personal-life link types (GBrain base pack vocabulary, adapted).

CREATE TABLE tl.page_kinds(
 kind text PRIMARY KEY CHECK (kind ~ '^[a-z][a-z0-9_-]{0,39}$'),
 description text NOT NULL DEFAULT '',
 sections text[] NOT NULL DEFAULT '{}',
 guidance text NOT NULL DEFAULT '',
 half_life_days int CHECK (half_life_days IS NULL OR half_life_days > 0),
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','candidate','retired')),
 origin text NOT NULL DEFAULT 'core' CHECK (origin IN ('core','lens','owner')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());

INSERT INTO tl.seed_hooks(name,ord,statement) VALUES
 ('page_kinds',20,$seed$INSERT INTO tl.page_kinds(kind,description,sections,guidance,half_life_days,status,origin) VALUES
 ('people','A person who matters to the owner',ARRAY['Who they are','Relationship','Life now','Tastes & preferences','Opinions & worldview','Important details','Open threads'],
  'First paragraph: who they are and how they relate to the owner, specifically (never "a contact"). Label soft claims (said / self-described / observed / inferred). A trait needs several observations; one is a Timeline line.',NULL,'active','core'),
 ('days','One owner-local day',ARRAY['Summary','People','Conversations','Plans & commitments','Decisions & ideas','Notable details'],
  'Slug days/YYYY-MM-DD. Link every person, event and place that featured. Carry forward open commitments that are still open.',14,'active','core'),
 ('events','A gathering, trip, appointment or meeting the owner attended or organized',ARRAY['What','Who was there','What happened','Decisions','Follow-ups','Notable quotes'],
  'Slug events/YYYY-MM-DD-name; set when. Only people the evidence places there (an invitation is not presence). Narrate in the true order.',90,'active','core'),
 ('places','A place the owner goes back to or that mattered',ARRAY['What it is','The owner and this place','Notable visits','Details'],
  'Slug places/name-city. Aliases for the owner''s own names for it ("the lake house").',NULL,'active','core'),
 ('organizations','A company, school, club or group',ARRAY['What it is','The owner''s connection','People there'],'',NULL,'active','core'),
 ('projects','Something the owner is building or working toward',ARRAY['What it is','Status','People','Decisions','Open questions'],'',90,'active','core'),
 ('topics','A recurring subject in the owner''s life',ARRAY['Summary','What the owner thinks','Related'],'Specific slugs (topics/sourdough-starter, not topics/cooking). Reuse an existing topic over coining a near-synonym.',NULL,'active','core'),
 ('ideas','An idea, thesis or framework the owner came up with',ARRAY['Summary','In their words','Analysis','Related'],'Keep the owner''s exact words in blockquotes; mark your reading as analysis.',NULL,'active','core'),
 ('reflections','The owner reflecting on themselves or their life',ARRAY['Summary','In their words','Analysis'],'Slug reflections/YYYY-MM-DD-topic. Never project emotional or therapeutic narratives the evidence does not show.',180,'active','core'),
 ('patterns','A theme that recurs across at least three reflections or days',ARRAY['The pattern','Instances','What it suggests'],'Say how many instances and over what span.',NULL,'active','core'),
 ('loops','Promises and pending asks with one counterparty',ARRAY['Owed by the owner','Owed to the owner','Waiting on a decision','Closed'],
  'Each item: what, when it was made, due only if stated, a verbatim quote and its citation. Close an item only when a record shows it was done, declined or dropped.',30,'active','core'),
 ('media','A book, film, show, album, game or article',ARRAY['What it is','The owner''s take','Who recommended it'],'',NULL,'active','core'),
 ('trips','A trip away from home',ARRAY['Where and when','Who','Itinerary','Highlights'],'Link the events and places of the trip with part_of.',NULL,'active','core'),
 ('questions','A standing question kept answered from evidence',ARRAY['Answer','Gaps'],'Every sentence cites evidence; name what the evidence does not answer.',NULL,'active','core')
 ON CONFLICT (kind) DO NOTHING$seed$),
 ('page_link_types',21,$seed$INSERT INTO tl.link_types(type,description,inverse,status,origin) VALUES
 ('family_of','Family of (say how in the link note)','family_of','active','core'),
 ('partner_of','Romantic partner of','partner_of','active','core'),
 ('friend_of','Friend of','friend_of','active','core'),
 ('lives_in','Lives or lived in a place','home_of','active','core'),
 ('works_at','Works or worked at','employs','active','core'),
 ('member_of','Member of a group or organization','has_member','active','core'),
 ('attended','Attended an event','attended_by','active','core'),
 ('hosted','Hosted or organized an event','hosted_by','active','core'),
 ('owes_to','Owes something to (a promise or a favour)','owed_by','active','core'),
 ('awaiting_reply_from','Waiting on a reply or decision from','owes_reply_to','active','core'),
 ('discusses','Discusses a topic or idea','discussed_in','active','core'),
 ('supersedes','Replaces an older page or plan','superseded_by','active','core')
 ON CONFLICT (type) DO NOTHING$seed$);
DO $$ BEGIN
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='page_kinds');
 EXECUTE (SELECT s.statement FROM tl.seed_hooks s WHERE s.name='page_link_types');
END $$;

CREATE TABLE tl.page_timeline(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 slug text NOT NULL REFERENCES tl.pages(slug) ON DELETE CASCADE ON UPDATE CASCADE,
 date date NOT NULL,
 summary text NOT NULL CHECK (length(btrim(summary)) BETWEEN 1 AND 2000),
 message_ids bigint[] NOT NULL DEFAULT '{}',
 written_by text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE UNIQUE INDEX page_timeline_dedup ON tl.page_timeline(slug, date, md5(summary));
CREATE INDEX page_timeline_date ON tl.page_timeline(date, slug);
CREATE INDEX page_timeline_messages ON tl.page_timeline USING gin(message_ids);

CREATE TABLE tl.page_redirects(
 old_slug text PRIMARY KEY,
 slug text NOT NULL REFERENCES tl.pages(slug) ON DELETE CASCADE ON UPDATE CASCADE,
 reason text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());

ALTER TABLE tl.pages ADD COLUMN truth_updated_at timestamptz;
UPDATE tl.pages SET truth_updated_at = updated_at;

ALTER TABLE tl.page_actions DROP CONSTRAINT IF EXISTS page_actions_op_check;
ALTER TABLE tl.page_actions ADD CONSTRAINT page_actions_op_check
  CHECK (op IN ('write','edit','timeline','link','unlink','merge','archive'));

REVOKE ALL ON tl.page_kinds, tl.page_timeline, tl.page_redirects FROM PUBLIC, throughline_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON tl.page_kinds, tl.page_timeline, tl.page_redirects TO throughline_app;
GRANT USAGE,SELECT ON SEQUENCE tl.page_timeline_id_seq TO throughline_app;
