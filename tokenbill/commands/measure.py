"""``tokenbill measure plan`` / ``measure run``: randomized rollouts and realized savings
(SPEC §13.2–§13.4).

``measure plan`` assigns clusters (cache-isolation units: workspaces, MDM groups, gateway groups;
never individuals) to seeded waves and a never-treated holdback, computes the washout, the MDE from
A/A re-randomizations of the pre-period panel and a pre-registration; ``--org-wide`` changes
without MDM or gateway clusters get an interrupted-time-series design (MEASURED at best).
``measure run`` estimates the realized saving per active developer-day at the pre-registered
baseline card with the guards (SRM, placebo, MDE, reconciliation, cache scope, quality, looks,
washout); exit 3 when no label applies.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.pipeline import savings as sv
from tokenbill.pipeline import verification as vf

VERB = "measure"
HELP = "measure realized savings: randomized rollout plans (plan) and estimates (run)"
_CLUSTER_KINDS = ("team", "workspace", "mdm_group", "gateway")


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``measure plan`` and ``measure run``."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    actions = parser.add_subparsers(dest="measure_action", metavar="ACTION", required=True)
    plan = actions.add_parser("plan", help="randomized rollout plan (or ITS design)",
                              description="Plan a randomized rollout of one lever.")
    sv.add_db(plan, help_text="ledger for the pre-period panel (MDE, clusters, washout)")
    sv.add_window(plan)
    plan.add_argument("--clusters", type=Path, metavar="FILE", default=None,
                      help="cluster ids (JSON array or one per line; default: the ledger's)")
    plan.add_argument("--cluster-kind", choices=_CLUSTER_KINDS, default="team",
                      help="cluster kind (default team)")
    plan.add_argument("--lever", required=True, metavar="ID", help="lever id to roll out")
    plan.add_argument("--design", choices=("cluster_rct", "stepped_wedge", "its"),
                      default="stepped_wedge", help="design (default stepped_wedge)")
    plan.add_argument("--waves", type=sv.positive_int, default=4, metavar="N",
                      help="number of waves (default 4)")
    plan.add_argument("--holdback", default="20", metavar="PCT",
                      help="never-treated holdback, 10–25%% (default 20)")
    plan.add_argument("--seed", type=sv.non_negative_int, default=0,
                      help="assignment seed (logged; default 0)")
    plan.add_argument("--projection", type=Path, metavar="FILE", default=None,
                      help="a findings/report/policy --format json output with the lever's "
                           "projected_monthly")
    plan.add_argument("--looks", default="", metavar="DATES",
                      help="pre-registered look dates, comma-separated YYYY-MM-DD")
    plan.add_argument("--treated", type=Path, metavar="FILE", default=None,
                      help="a user assignment (JSON cluster → wave or control): MEASURED at best")
    plan.add_argument("--org-wide", action="store_true",
                      help="the setting is delivered org-wide (server-managed)")
    plan.add_argument("--change-date", type=sv.date_arg, metavar="DATE", default=None,
                      help="ITS: the pre-registered change date")
    plan.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE",
                      default=None, help="write plan.json")
    sv.add_format(plan)
    run = actions.add_parser("run", help="realized savings of a plan",
                             description="Estimate the realized saving of a planned rollout.")
    sv.add_db(run, required=True)
    sv.add_window(run)
    run.add_argument("--plan", required=True, type=Path, metavar="FILE",
                     help="the plan.json of measure plan")
    run.add_argument("--baseline-rates", type=Path, metavar="FILE", action="append",
                     default=None, help="rate file(s) of the pre-registered baseline card")
    run.add_argument("--panel", dest="panel_name", metavar="NAME", default=None,
                     help="an extension's panel (e.g. copilot)")
    run.add_argument("--allowance", action="store_true",
                     help="measure the allowance (list-equivalent) class; never signable")
    run.add_argument("--seed", type=sv.non_negative_int, default=0,
                     help="bootstrap seed (default 0)")
    run.add_argument("--boot", type=sv.positive_int, default=2000,
                     help="bootstrap resamples (default 2000)")
    run.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE",
                     default=None, help="write measurement.json")
    sv.add_format(run)
    return parser


def _plan(args: argparse.Namespace) -> int:
    env = sv.env_from_args(args)
    store, _path = sv.open_ledger(args, env, required=False)
    try:
        projection = vf.load_projection(args.projection, args.lever) if args.projection else None
        change = sv._date(args.change_date) if args.change_date is not None else None
        result = vf.run_measure_plan(
            env, lever_id=args.lever, cluster_kind=args.cluster_kind, design=args.design,
            waves=args.waves, holdback=vf.parse_holdback(args.holdback), seed=args.seed,
            store=store, clusters=vf.load_clusters(args.clusters) if args.clusters else None,
            projection=projection, looks=vf.parse_looks(args.looks),
            treated=vf.load_treated(args.treated) if args.treated else None,
            org_wide=args.org_wide, since_ms=args.since, until_ms=args.until,
            change_date=change)
    finally:
        sv.close_ledger(store)
    assert result.measure_plan is not None
    if args.output is not None:
        sv.write_text(args.output, sv.json_text(vf.plan_json(result.measure_plan)))
    sv.emit(result, args)
    return sv.dq_exit(result, args, env)


def _run(args: argparse.Namespace) -> int:
    env = sv.env_from_args(args)
    plan, log = vf.load_plan(args.plan)
    base = None
    if args.baseline_rates:
        from tokenbill.pipeline.common import build_env

        base = build_env(env.config, rates=tuple(args.baseline_rates),
                         pricer_factory=sv.PRICER_FACTORY).pricer
    store, path = sv.open_ledger(args, env)
    try:
        result = vf.run_measure_run(env, plan=plan, assignment_log=log, store=store,
                                    db_path=path, baseline_pricer=base,
                                    panel_name=args.panel_name, since_ms=args.since,
                                    until_ms=args.until, seed=args.seed, boot=args.boot,
                                    billing_class="allowance" if args.allowance else "billed")
    finally:
        sv.close_ledger(store)
    m = result.measurements[0]
    if args.output is not None:
        cal = plan.projection.calibration.value if plan.projection is not None else "n/a"
        verdict = result.reconciliation.verdict if result.reconciliation else "insufficient_data"
        sv.write_text(args.output, sv.json_text(vf.measurement_json(
            m, reconciliation_verdict=verdict, calibration=cal)))
    sv.emit(result, args)
    return sv.dq_exit(result, args, env)


def run(args: argparse.Namespace) -> int:
    """Execute ``measure plan`` or ``measure run``."""
    def body() -> int:
        action = getattr(args, "measure_action", None)
        if action == "plan":
            return _plan(args)
        if action == "run":
            return _run(args)
        raise UsageError("measure needs an action: plan or run")

    return sv.run_command(body, verb=VERB)
