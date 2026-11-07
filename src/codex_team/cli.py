from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .cluster import (
    ClusterManager,
    daemon_status,
    ensure_daemon,
    stop_daemon,
)
from .dashboard import serve as serve_dashboard
from .service import TeamError, TeamService
from .worktrees import WorktreeManager


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _task_id(value: str) -> str:
    return value.strip().upper()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teamctl",
        description="Codex team board, worktree coordinator, and dashboard",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize a Git repository for Codex team collaboration")
    init.add_argument("--repo", default=".")
    init.add_argument("--max-threads", type=int, choices=range(1, 9))

    status = sub.add_parser("status", help="Show board status")
    status.add_argument("--repo")

    task = sub.add_parser("task", help="Manage tasks")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    create = task_sub.add_parser("create")
    create.add_argument("--repo", default=".")
    create.add_argument("--title", required=True)
    create.add_argument("--description", required=True)
    create.add_argument("--acceptance", required=True)
    create.add_argument("--kind", required=True, choices=["read", "write", "review", "ops"])
    create.add_argument("--owner-role", required=True)
    create.add_argument("--goal-id")
    create.add_argument("--actor", default="lead")
    create.add_argument("--priority", type=int, default=2, choices=range(0, 4))
    create.add_argument("--depends-on", action="append", default=[])

    listing = task_sub.add_parser("list")
    listing.add_argument("--repo", default=".")
    listing.add_argument("--status", action="append", dest="statuses")
    listing.add_argument("--owner-role")
    listing.add_argument("--goal-id")
    listing.add_argument("--active-only", action="store_true")
    listing.add_argument("--include-archived", action="store_true")

    show = task_sub.add_parser("show")
    show.add_argument("task_id")

    events = task_sub.add_parser("events")
    events.add_argument("task_id")
    events.add_argument("--limit", type=int, default=100)

    claim = task_sub.add_parser("claim")
    claim.add_argument("task_id")
    claim.add_argument("--actor", required=True)
    claim.add_argument("--lease-minutes", type=int, default=30)

    heartbeat = task_sub.add_parser("heartbeat")
    heartbeat.add_argument("task_id")
    heartbeat.add_argument("--actor", required=True)
    heartbeat.add_argument("--token", required=True)
    heartbeat.add_argument("--lease-minutes", type=int, default=30)

    update = task_sub.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("--actor", required=True)
    update.add_argument("--token", required=True)
    update.add_argument("--note", required=True)
    update.add_argument("--result")
    update.add_argument("--tests")

    submit = task_sub.add_parser("submit")
    submit.add_argument("task_id")
    submit.add_argument("--actor", required=True)
    submit.add_argument("--token", required=True)
    submit.add_argument("--result", required=True)
    submit.add_argument("--tests", required=True)
    submit.add_argument("--commit", default="")

    block = task_sub.add_parser("block")
    block.add_argument("task_id")
    block.add_argument("--actor", required=True)
    block.add_argument("--reason", required=True)
    block.add_argument("--token")

    unblock = task_sub.add_parser("unblock")
    unblock.add_argument("task_id")
    unblock.add_argument("--actor", default="lead")

    cancel = task_sub.add_parser("cancel")
    cancel.add_argument("task_id")
    cancel.add_argument("--actor", default="lead")
    cancel.add_argument("--reason", required=True)

    review = task_sub.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("--actor", required=True, choices=["lead", "main", "human"])
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    review.add_argument("--notes", required=True)
    review.add_argument("--merge-commit", default="")

    role = sub.add_parser("role", help="Inspect portable role prompt packs")
    role_sub = role.add_subparsers(dest="role_command", required=True)
    role_sub.add_parser("list")
    role_show = role_sub.add_parser("show")
    role_show.add_argument(
        "role",
        choices=["team_explorer", "team_worker", "team_reviewer"],
    )
    role_show.add_argument("--repo")

    worktree = sub.add_parser("worktree", help="Manage isolated write worktrees")
    worktree_sub = worktree.add_subparsers(dest="worktree_command", required=True)
    wt_create = worktree_sub.add_parser("create")
    wt_create.add_argument("task_id")
    wt_create.add_argument("--repo", default=".")
    wt_create.add_argument("--actor", default="lead")
    wt_create.add_argument("--base-ref", default="HEAD")
    wt_create.add_argument("--path")
    wt_list = worktree_sub.add_parser("list")
    wt_list.add_argument("--repo", default=".")

    message = sub.add_parser("message", help="Manage team messages")
    message_sub = message.add_subparsers(dest="message_command", required=True)
    msg_add = message_sub.add_parser("add")
    msg_add.add_argument("--repo", default=".")
    msg_add.add_argument("--sender", required=True)
    msg_add.add_argument("--body", required=True)
    msg_add.add_argument("--recipient")
    msg_add.add_argument("--task-id")
    msg_list = message_sub.add_parser("list")
    msg_list.add_argument("--repo", default=".")
    msg_list.add_argument("--task-id")
    msg_list.add_argument("--limit", type=int, default=50)

    cluster = sub.add_parser("cluster", help="Manage the automatic agent cluster")
    cluster_sub = cluster.add_subparsers(dest="cluster_command", required=True)
    cluster_start = cluster_sub.add_parser("start")
    cluster_start.add_argument("--repo", default=".")
    cluster_start.add_argument("--objective", required=True)
    cluster_start.add_argument("--autonomy", choices=["auto", "supervised"], default="auto")
    cluster_start.add_argument("--max-worker-attempts", type=int, default=3)
    cluster_start.add_argument("--max-lead-attempts", type=int, default=5)
    cluster_start.add_argument("--no-daemon", action="store_true")
    cluster_sub.add_parser("list")
    cluster_status = cluster_sub.add_parser("status")
    cluster_status.add_argument("goal_id", nargs="?")
    cluster_pause = cluster_sub.add_parser("pause")
    cluster_pause.add_argument("goal_id")
    cluster_resume = cluster_sub.add_parser("resume")
    cluster_resume.add_argument("goal_id")
    cluster_cancel = cluster_sub.add_parser("cancel")
    cluster_cancel.add_argument("goal_id")
    cluster_sub.add_parser("daemon-status")
    cluster_sub.add_parser("stop-daemon")
    daemon = cluster_sub.add_parser("daemon")
    daemon.add_argument("--interval", type=float, default=5.0)
    daemon.add_argument("--once", action="store_true")

    dashboard = sub.add_parser("dashboard", help="Serve the local visual task board")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-browser", action="store_true")

    sub.add_parser("serve", help="Run the Codex MCP stdio server")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    team = TeamService(settings)
    worktrees = WorktreeManager(team)

    try:
        if args.command == "init":
            _print(team.init_project(args.repo, max_threads=args.max_threads))
        elif args.command == "status":
            if args.repo:
                _print(team.board_snapshot(args.repo))
            else:
                _print(team.dashboard_snapshot())
        elif args.command == "task":
            if args.task_command == "create":
                _print(
                    team.create_task(
                        args.repo,
                        title=args.title,
                        description=args.description,
                        acceptance=args.acceptance,
                        kind=args.kind,
                        owner_role=args.owner_role,
                        actor=args.actor,
                        priority=args.priority,
                        depends_on=args.depends_on,
                        goal_id=args.goal_id,
                    )
                )
            elif args.task_command == "list":
                _print(
                    {
                        "tasks": team.list_tasks(
                            args.repo,
                            statuses=args.statuses,
                            owner_role=args.owner_role,
                            goal_id=args.goal_id,
                            include_done=not args.active_only,
                            include_archived=args.include_archived,
                        )
                    }
                )
            elif args.task_command == "show":
                _print(team.get_task(_task_id(args.task_id)))
            elif args.task_command == "events":
                _print(team.get_task_events(_task_id(args.task_id), limit=args.limit))
            elif args.task_command == "claim":
                _print(
                    team.claim_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        lease_minutes=args.lease_minutes,
                    )
                )
            elif args.task_command == "heartbeat":
                _print(
                    team.heartbeat(
                        _task_id(args.task_id),
                        actor=args.actor,
                        claim_token=args.token,
                        lease_minutes=args.lease_minutes,
                    )
                )
            elif args.task_command == "update":
                _print(
                    team.update_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        claim_token=args.token,
                        note=args.note,
                        result=args.result,
                        tests=args.tests,
                        commit_sha=args.commit,
                    )
                )
            elif args.task_command == "submit":
                _print(
                    team.submit_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        claim_token=args.token,
                        result=args.result,
                        tests=args.tests,
                        commit_sha=args.commit,
                    )
                )
            elif args.task_command == "block":
                _print(
                    team.block_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        reason=args.reason,
                        claim_token=args.token,
                    )
                )
            elif args.task_command == "unblock":
                _print(
                    team.unblock_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                    )
                )
            elif args.task_command == "cancel":
                _print(
                    team.cancel_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        reason=args.reason,
                    )
                )
            elif args.task_command == "review":
                if args.approve == args.reject:
                    raise TeamError("choose exactly one review decision")
                _print(
                    team.review_task(
                        _task_id(args.task_id),
                        actor=args.actor,
                        approve=args.approve,
                        notes=args.notes,
                        merge_commit=args.merge_commit,
                    )
                )
        elif args.command == "role":
            if args.role_command == "list":
                _print({"roles": team.list_roles()})
            elif args.role_command == "show":
                _print(team.role_prompt(args.role, args.repo))
        elif args.command == "worktree":
            if args.worktree_command == "create":
                _print(
                    worktrees.create(
                        _task_id(args.task_id),
                        repo=args.repo,
                        actor=args.actor,
                        base_ref=args.base_ref,
                        worktree_path=args.path,
                    )
                )
            elif args.worktree_command == "list":
                _print({"worktrees": worktrees.list(args.repo)})
        elif args.command == "message":
            if args.message_command == "add":
                _print(
                    team.add_message(
                        args.repo,
                        sender=args.sender,
                        body=args.body,
                        recipient=args.recipient,
                        task_id=args.task_id,
                    )
                )
            elif args.message_command == "list":
                _print(
                    {
                        "messages": team.list_messages(
                            args.repo,
                            task_id=args.task_id,
                            limit=args.limit,
                        )
                    }
                )
        elif args.command == "cluster":
            cluster = ClusterManager(settings, team=team, worktrees=worktrees)
            if args.cluster_command == "start":
                goal = cluster.create_goal(
                    args.repo,
                    objective=args.objective,
                    autonomy=args.autonomy,
                    max_worker_attempts=args.max_worker_attempts,
                    max_lead_attempts=args.max_lead_attempts,
                )
                daemon = None if args.no_daemon else ensure_daemon(settings)
                _print({"goal": goal, "daemon": daemon})
            elif args.cluster_command == "list":
                _print({"goals": cluster.list_goals()})
            elif args.cluster_command == "status":
                if args.goal_id:
                    _print(cluster.get_goal(args.goal_id))
                else:
                    _print({"goals": cluster.list_goals(), "daemon": daemon_status(settings)})
            elif args.cluster_command == "pause":
                _print(cluster.set_goal_status(args.goal_id, "paused", reason="paused by user"))
            elif args.cluster_command == "resume":
                _print(cluster.set_goal_status(args.goal_id, "active", reason="resumed by user"))
                _print({"daemon": ensure_daemon(settings)})
            elif args.cluster_command == "cancel":
                _print(cluster.cancel_goal(args.goal_id, reason="canceled by user"))
            elif args.cluster_command == "daemon-status":
                _print(daemon_status(settings))
            elif args.cluster_command == "stop-daemon":
                _print(stop_daemon(settings))
            elif args.cluster_command == "daemon":
                from .clusterd import run as cluster_daemon_run

                return cluster_daemon_run(interval=args.interval, once=args.once)
        elif args.command == "dashboard":
            serve_dashboard(
                settings,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
        elif args.command == "serve":
            from .mcp import main as mcp_main

            return mcp_main()
        else:
            raise TeamError(f"unknown command: {args.command}")
    except (TeamError, OSError, ValueError) as exc:
        _print({"error": str(exc)})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
