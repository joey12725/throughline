"""Pure scorers: no I/O, no clock, no randomness.

`returned` is always an ordered list of delivered units. A unit is one record
reference, or a collection of references for a span (a returned stretch of
context that contains several messages). References are compared by equality,
so scorers work on gold keys and on bound message ids alike. `question` is a
bench.gold.Question or anything with the same attributes.
"""

import math

SPAN_CREDIT_DEFAULT = True


def _refs(unit):
    if isinstance(unit, (set, frozenset)):
        return frozenset(unit)
    if isinstance(unit, list):
        return frozenset(unit)
    return frozenset([unit])


def with_spans(records, spans=None, span_credit=SPAN_CREDIT_DEFAULT):
    """Delivered units from an arm result.

    With span credit (the default, recorded with every run) a span that
    contains a gold message counts as delivering it; without, only the record
    the system named counts. `spans` maps a record to the members of its span.
    """
    spans = spans or {}
    units = []
    for record in records:
        if span_credit and spans.get(record):
            units.append(frozenset(spans[record]) | {record})
        else:
            units.append(record)
    return units


def _delivered(returned):
    out = set()
    for unit in returned:
        out |= _refs(unit)
    return out


def precision_recall(returned, question):
    """Group-aware precision and recall.

    A required group is one true positive however many of its alternatives
    are returned. Helpful records and members of optional groups are neither
    credited nor penalised. Every other returned unit is a false positive.
    """
    units = [_refs(u) for u in returned]
    delivered = _delivered(units)
    required = [g for g in question.groups if g.required]
    hit = sum(1 for g in required if delivered.intersection(g.alternatives))
    neutral = set(question.helpful)
    for group in question.groups:
        neutral.update(group.alternatives)
    for cluster in getattr(question, "clusters", []):
        neutral.update(cluster.members)
        neutral.update(cluster.ambiguous)
    wrong = sum(1 for unit in units if not unit & neutral)
    precision = hit / (hit + wrong) if hit + wrong else 0.0
    recall = hit / len(required) if required else 0.0
    return precision, recall


def f_beta(returned, question, beta=0.5):
    """F-beta over evidence groups; beta 0.5 weighs precision twice recall.

    Undefined for a question without required groups (score those with
    abstention); 0.0 is returned so a caller that forgets cannot gain from it.
    """
    precision, recall = precision_recall(returned, question)
    if not precision or not recall:
        return 0.0
    b = beta * beta
    return (1 + b) * precision * recall / (b * precision + recall)


def group_coverage(returned, question):
    """BEN-10: does the context hold a source for every required group."""
    delivered = _delivered(returned)
    required = [g for g in question.groups if g.required]
    covered = [g.id for g in required if delivered.intersection(g.alternatives)]
    missing = [g.id for g in required if g.id not in covered]
    return dict(
        all_required_covered=bool(required) and not missing,
        fraction=len(covered) / len(required) if required else 0.0,
        covered=covered,
        missing=missing,
    )


def recall_at_k(returned, question, k):
    """Share of required groups covered by the first k units."""
    return group_coverage(list(returned)[:k], question)["fraction"]


def stage_recall(stages, question):
    """BEN-9: group recall of the candidates each stage held.

    `stages` maps a stage name (for example before_rerank, after_rerank,
    after_selection) to its candidates. A stage the system did not report is
    simply absent from the result.
    """
    return {
        name: group_coverage(candidates, question)["fraction"]
        for name, candidates in stages.items()
        if candidates is not None
    }


def cluster_coverage(returned, question):
    """BEN-13: share of gold clusters with a certain member delivered.

    An ambiguous member alone does not cover its cluster; `uncertain` lists
    clusters reached only that way so missing cases and uncertainty are told
    apart.
    """
    delivered = _delivered(returned)
    covered, uncertain, missing = [], [], []
    for cluster in question.clusters:
        if delivered.intersection(cluster.members):
            covered.append(cluster.id)
        elif delivered.intersection(cluster.ambiguous):
            uncertain.append(cluster.id)
        else:
            missing.append(cluster.id)
    total = len(question.clusters)
    return dict(
        fraction=len(covered) / total if total else 0.0,
        covered=covered,
        uncertain=uncertain,
        missing=missing,
    )


def duplicate_grouping_accuracy(predicted, question):
    """BEN-13: pairwise agreement of predicted groups with gold clusters.

    `predicted` is a list of groups of references. Only certain cluster
    members that appear in the prediction are judged; a pair is right when
    "same predicted group" equals "same gold cluster". Ambiguous members may
    go anywhere. None when fewer than two judged records (nothing to grade).
    """
    gold = {}
    for cluster in question.clusters:
        for ref in cluster.members:
            gold[ref] = cluster.id
    assigned = {}
    for number, group in enumerate(predicted):
        for ref in _refs(group):
            if ref in gold:
                assigned.setdefault(ref, number)
    judged = sorted(assigned, key=repr)
    pairs = right = 0
    for i, a in enumerate(judged):
        for b in judged[i + 1 :]:
            pairs += 1
            right += (assigned[a] == assigned[b]) == (gold[a] == gold[b])
    return right / pairs if pairs else None


def role_coverage(returned, question, min_per_role=1):
    """BEN-14: share of gold populations with at least min_per_role records.

    Only whether the comparison population, the supporting observations and
    the counterexamples were supplied is scored, never an interpretation.
    """
    delivered = _delivered(returned)
    counts = {
        name: len(delivered.intersection(members))
        for name, members in question.roles.items()
    }
    met = [name for name, count in counts.items() if count >= min_per_role]
    return dict(
        fraction=len(met) / len(counts) if counts else 0.0,
        counts=counts,
        missing=[name for name in counts if name not in met],
    )


def abstention(returned, question, abstained=None):
    """BEN-20: was answering or abstaining the right call.

    A system abstains by returning no evidence unit other than helpful ones,
    or by saying so (`abstained`).
    """
    helpful = set(question.helpful)
    substantive = [u for u in returned if not _refs(u) <= helpful]
    silent = (not substantive) if abstained is None else bool(abstained)
    if question.no_answer:
        outcome = "correct_abstention" if silent else "false_answer"
    else:
        outcome = "false_abstention" if silent else "answered"
    return dict(
        expected=bool(question.no_answer),
        abstained=silent,
        correct=outcome in ("correct_abstention", "answered"),
        outcome=outcome,
    )


def incomplete_disclosure(incomplete, disclosures, question):
    """BEN-20: an incomplete search must be declared, and only then.

    `incomplete` is the system's own flag; `disclosures` the reasons it gave.
    Each expected term must occur in some reason (case-insensitive substring).
    """
    said = [str(d).lower() for d in (disclosures or [])]
    absent = [
        term
        for term in question.expects_disclosure
        if not any(term.lower() in d for d in said)
    ]
    expected = bool(question.expects_incomplete)
    flagged = bool(incomplete)
    # Declaring a complete search incomplete is wrong too ("and only then"):
    # a system that always hedges must not score as if it disclosed.
    false_alarm = flagged and not expected
    return dict(
        expected=expected,
        declared=flagged,
        missing_terms=absent,
        false_alarm=false_alarm,
        correct=flagged == expected and not absent,
    )


def leakage(returned, question, stages=None):
    """BEN-21: forbidden records in the result or in any reported stage."""
    forbidden = set(question.forbidden)
    in_records = sorted(_delivered(returned) & forbidden, key=repr)
    in_stages = {}
    for name, candidates in (stages or {}).items():
        found = sorted(_delivered(candidates or []) & forbidden, key=repr)
        if found:
            in_stages[name] = found
    return dict(
        clean=not in_records and not in_stages,
        records=in_records,
        stages=in_stages,
        count=len(in_records) + sum(len(v) for v in in_stages.values()),
    )


def citation_faithfulness(cited_ids, delivered_ids, gold):
    """Share of cited ids that exist, were delivered and are gold evidence.

    An id that was never delivered is treated as not existing for the reader:
    it cannot be checked. Nothing cited scores 1.0 only when there is nothing
    to cite (a no-answer question), else 0.0.
    """
    cited = list(dict.fromkeys(cited_ids))
    if not cited:
        return 1.0 if not gold.required_groups() else 0.0
    delivered = set(delivered_ids)
    evidence = gold.gold_refs()
    return sum(1 for c in cited if c in delivered and c in evidence) / len(cited)


def percentiles(values, points=(50, 95)):
    """Linear-interpolation percentiles: {"p50": ..., "p95": ...}."""
    ordered = sorted(float(v) for v in values)
    out = {}
    for point in points:
        if not ordered:
            out[f"p{point}"] = None
            continue
        position = (len(ordered) - 1) * point / 100
        low, high = math.floor(position), math.ceil(position)
        out[f"p{point}"] = ordered[low] + (ordered[high] - ordered[low]) * (
            position - low
        )
    return out


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


# --- Behaviour scorers ---------------------------------------------------------


def incident_count_exact(manifest, question):
    """episode_count: the manifest counts incidents, not hits.

    `manifest` is a recall manifest {by, unit, incidents, records, ...}. Exact
    when grouped by episode with unit 'episode' and incidents equal to
    question.expects_incidents. A record count in the incidents field (or no
    manifest at all) is never exact.
    """
    manifest = manifest or {}
    expected = question.expects_incidents
    incidents = manifest.get("incidents")
    unit = manifest.get("unit")
    return dict(
        expected=expected,
        incidents=incidents,
        records=manifest.get("records"),
        unit=unit,
        exact=expected is not None and unit == "episode" and incidents == expected,
    )


def newest_relevant_top1(returned, question, newest_absolute=None):
    """newest_relevant: the first unit is the newest record that answers.

    `newest_absolute` is the newest record of the thread whatever it says; it
    must never come first unless it is the expected one.
    """
    first = _refs(returned[0]) if returned else frozenset()
    expected = question.expects_newest
    top1 = expected is not None and expected in first
    absolute_first = (
        newest_absolute is not None
        and newest_absolute != expected
        and newest_absolute in first
    )
    return dict(top1=top1 and not absolute_first, absolute_first=absolute_first)


NEGATIVE_ASSERTIONS = ("did_not_happen", "never_happened", "no_such_event")


def absence_disclosed(execution, question):
    """coverage_honesty: an empty result states the right kind of absence.

    Reads execution.coverage.absence and execution.warnings. Correct when the
    statement equals question.expects_absence_statement, the warning
    'absence_is_not_evidence' accompanies every statement other than
    'not_found_in_covered_sources', and nothing asserts that the event did
    not happen.
    """
    execution = execution or {}
    coverage = execution.get("coverage") or {}
    statement = coverage.get("absence")
    warnings = [str(w) for w in execution.get("warnings") or []]
    said = " ".join([str(statement)] + warnings).lower()
    negative = any(term in said for term in NEGATIVE_ASSERTIONS)
    expected = question.expects_absence_statement
    warned = "absence_is_not_evidence" in warnings
    needs_warning = expected not in (None, "not_found_in_covered_sources")
    return dict(
        expected=expected,
        statement=statement,
        warned=warned,
        negative_assertion=negative,
        correct=statement == expected
        and not negative
        and (warned or not needs_warning),
    )


def dyad_both_sides(authors, dyad):
    """dyad_vs_author: share of the two people whose messages were returned.

    `authors` are the person keys of the returned records (None for records
    without an attributed author); 1.0 means both sides are present, 0.5 one.
    """
    if not dyad:
        return 0.0
    present = set(authors or ()) & set(dyad)
    return len(present) / len(set(dyad))


def hidden_count_leak(restricted, expected_visible):
    """restricted_inference: how far an aggregate seen by a restricted asker
    differs from the same aggregate over the rows that asker may see.

    Both arguments are numbers (a count, a rank position) or mappings of name
    to number; 0 means the hidden rows left no trace. A missing value on the
    restricted side counts as no leak only when nothing was expected.
    """
    if isinstance(restricted, dict) or isinstance(expected_visible, dict):
        restricted, expected_visible = restricted or {}, expected_visible or {}
        return sum(
            hidden_count_leak(restricted.get(k), expected_visible.get(k))
            for k in set(restricted) | set(expected_visible)
        )
    return abs((restricted or 0) - (expected_visible or 0))
