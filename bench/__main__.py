"""python -m bench <suite>: the twin benchmark against a running system.

The target is named by THROUGHLINE_URL and THROUGHLINE_TOKEN (an owner
token). --load ingests the synthetic corpus first (and plants provenance,
claims, link validity and coverage), so point it only at a disposable
instance; without --load the corpus must already be there. The target's own
maintenance (its embedding worker) must have run after a load for meaning
legs and page passages to be available; the harness cannot start it.

Suites: run (wave-1 arm matrix), matrix (v1.1 ablation matrix), resilience,
temporal, latency, librarian-value, portability, behaviours. Every suite can append
its report to a scoreboard (--scoreboard), with the named configuration
(--config, see bench.configs) and the commit.

behaviours runs all twelve behaviour scenarios and reports each with its status
(passed, failed, or skipped with the missing need named) and metrics; with
--scoreboard it appends the suite row and then one 'behaviour' row per
scenario. Behaviours that need other callers (an agent or plugin token, a
restricted asker, labels) are skipped unless --local-principals is given,
which creates them through this process's owner database connection: only
for a disposable instance that runs on this machine.
"""

import argparse
import json
import os
import sys

import httpx

from . import configs, latency, portability, run as runner, scenarios, scoreboard

SUITES = (
    "run",
    "matrix",
    "resilience",
    "temporal",
    "latency",
    "librarian-value",
    "portability",
    "behaviours",
)


def _client(args):
    url = args.url or os.environ.get("THROUGHLINE_URL")
    token = os.environ.get("THROUGHLINE_TOKEN")
    if not url or not token:
        sys.exit("set THROUGHLINE_URL (or --url) and THROUGHLINE_TOKEN")
    return httpx.Client(
        base_url=url.rstrip("/").removesuffix("/v1"),
        headers={"Authorization": "Bearer " + token},
        timeout=120,
        trust_env=False,
    )


def _arms(value):
    return [name for name in value.split(",") if name] if value else None


def _parser():
    parser = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in SUITES:
        sub = commands.add_parser(name)
        sub.add_argument("--url")
        sub.add_argument("--seed", type=int, default=47)
        sub.add_argument("--scale", type=int, default=1)
        sub.add_argument("--arms", help="comma-separated arm names")
        sub.add_argument("--load", action="store_true", help="ingest the corpus first")
        sub.add_argument("--scoreboard", help="append the result to this JSONL file")
        sub.add_argument("--json", action="store_true")
        sub.add_argument(
            "--config",
            default="deployment",
            choices=sorted(configs.CONFIGS),
            help="the configuration the target runs with (recorded)",
        )
    latency_cmd = commands.choices["latency"]
    latency_cmd.add_argument("--baseline", help="baseline JSON file")
    latency_cmd.add_argument(
        "--write-baseline", action="store_true", help="record this run as baseline"
    )
    latency_cmd.add_argument("--repeats", type=int, default=3)
    latency_cmd.add_argument("--warmup", type=int, default=1)
    latency_cmd.add_argument("--tolerance", type=float, default=latency.TOLERANCE)
    latency_cmd.add_argument(
        "--operations",
        action="store_true",
        help="time recall, expansion, match and librarian apart (BEN-19)",
    )
    commands.choices["librarian-value"].add_argument(
        "--threshold", type=float, default=runner.TIE_POINTS
    )
    behaviours_cmd = commands.choices["behaviours"]
    behaviours_cmd.add_argument(
        "--local-principals",
        action="store_true",
        help="create tokens, askers and labels through the local owner connection",
    )
    behaviours_cmd.add_argument("--behaviours", help="comma-separated scenario names")
    return parser


def _behaviour_rows(report):
    """One scoreboard row per behaviour scenario: status, reason and metrics."""
    for name, out in report["behaviours"].items():
        yield dict(
            suite="behaviour",
            behaviour=name,
            status=out["status"],
            reason=out.get("reason"),
            missing=out.get("missing"),
            needs=out.get("needs"),
            checked_by=out.get("checked_by"),
            metrics=out.get("metrics"),
            disclosures=out.get("disclosures"),
            planted=out.get("planted"),
            ms=out.get("ms"),
            config=report.get("config"),
            harness=report.get("harness"),
        )


def _emit(args, report, text=None):
    print(json.dumps(report, indent=2, default=str) if args.json or not text else text)
    if args.scoreboard:
        scoreboard.append(args.scoreboard, report)


def _latency(args, client, corpus):
    questions, _, _ = runner.prepare(client, corpus, load=args.load)
    if args.operations:
        sample = questions[:: max(1, len(questions) // 20)]
        measured = latency.measure_operations(
            client, sample, repeats=args.repeats, warmup=args.warmup
        )
        numbers = {
            name: {m: value.get(m) for m in latency.METRICS}
            for name, value in measured.items()
            if value.get("status") == "ok"
        }
    else:
        measured = latency.measure(
            client,
            questions,
            _arms(args.arms),
            repeats=args.repeats,
            warmup=args.warmup,
        )
        numbers = measured
    report = dict(
        suite="latency",
        seed=args.seed,
        scale=args.scale,
        config=configs.record(args.config),
        operations=measured,
    )
    baseline = latency.read_baseline(args.baseline) if args.baseline else None
    if baseline is not None and not args.write_baseline:
        report["gate"] = latency.compare(baseline, numbers, args.tolerance)
    if args.baseline and (args.write_baseline or baseline is None):
        # The first recorded run sets the targets (BEN-19).
        latency.write_baseline(args.baseline, numbers)
        report["baseline_written"] = args.baseline
    _emit(args, report)
    return 0 if report.get("gate", {}).get("ok", True) else 1


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "behaviours" and args.behaviours:
        from .behaviours import BEHAVIOURS

        unknown = [n for n in _arms(args.behaviours) if n not in BEHAVIOURS]
        if unknown:
            parser.error(
                "unknown behaviour: "
                + ", ".join(unknown)
                + " (known: "
                + ", ".join(BEHAVIOURS)
                + ")"
            )
    corpus = scenarios.build(args.seed, args.scale)
    with _client(args) as client:
        if args.command == "run":
            report, _ = runner.run(client, corpus, _arms(args.arms), load=args.load)
            _emit(args, report, None if args.json else runner.render(report))
            return 0
        if args.command == "latency":
            return _latency(args, client, corpus)
        questions, phase_times, _ = runner.prepare(client, corpus, load=args.load)
        available = runner.arm_registry.probe(client)
        config = args.config
        if args.command == "matrix":
            report, _ = runner.matrix(
                client, questions, available, _arms(args.arms), config=config
            )
        elif args.command == "resilience":
            report = runner.resilience(client, questions, available, config=config)
        elif args.command == "temporal":
            report = runner.temporal(client, questions, available, config=config)
        elif args.command == "librarian-value":
            report = runner.librarian_value(
                client, questions, available, threshold=args.threshold, config=config
            )
        elif args.command == "portability":
            sample = [q for q in questions if not q.needs][:40]
            report = portability.compare(
                [portability.HttpRunner(client), portability.CliRunner(client)], sample
            )
            report["config"] = configs.record(config)
        else:
            # Behaviours that need other callers (tokens, askers) are skipped
            # with that need named: they are created by the owner, not HTTP.
            from . import behaviours

            principals = behaviours.LocalPrincipals() if args.local_principals else None
            report = runner.behaviour_suite(
                client,
                corpus,
                phase_times,
                principals=principals,
                available=available,
                config=config,
                names=_arms(args.behaviours),
            )
            _emit(args, report)
            if args.scoreboard:
                for row in _behaviour_rows(report):
                    scoreboard.append(args.scoreboard, row)
            return 0
        _emit(args, report)
        return 0


if __name__ == "__main__":
    sys.exit(main())
