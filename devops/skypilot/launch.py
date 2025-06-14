#!/usr/bin/env -S uv run
import argparse
import copy
import shlex
import subprocess

import sky

from devops.skypilot.utils import launch_task


def patch_task(
    task: sky.Task,
    cpus: int | None,
    gpus: int | None,
    nodes: int | None,
    no_spot: bool = False,
    timeout_hours: float | None = None,
) -> sky.Task:
    overrides = {}
    if cpus:
        overrides["cpus"] = cpus
    if overrides:
        task.set_resources_override(overrides)
    if nodes:
        task.num_nodes = nodes

    new_resources_list = list(task.resources)

    if gpus:
        new_resources_list = []
        for res in list(task.resources):
            if not isinstance(res.accelerators, dict):
                # shouldn't happen with our current config
                raise Exception(f"Unexpected accelerator type: {res.accelerators}, {type(res.accelerators)}")

            patched_accelerators = copy.deepcopy(res.accelerators)
            patched_accelerators = {gpu_type: gpus for gpu_type in patched_accelerators.keys()}
            new_resources = res.copy(accelerators=patched_accelerators)
            new_resources_list.append(new_resources)

    if no_spot:
        new_resources_list = [res.copy(use_spot=False) for res in new_resources_list]

    if gpus or no_spot:
        task.set_resources(type(task.resources)(new_resources_list))

    # Add timeout configuration if specified
    if timeout_hours is not None:
        current_run_script = task.run or ""
        # Construct the command parts
        # timeout utility takes DURATION COMMAND [ARG]...
        # Here, COMMAND is 'bash', and its ARGs are '-c' and the script itself.
        timeout_command_parts = [
            "timeout",
            f"{timeout_hours}h",  # Use 'h' suffix for hours, timeout supports floats
            "bash",
            "-c",
            current_run_script,
        ]
        # shlex.join will correctly quote each part, especially current_run_script,
        # ensuring it's passed as a single argument to bash -c.
        task.run = shlex.join(timeout_command_parts)

    return task


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", help="Command to run")
    parser.add_argument("run", help="Run ID")
    parser.add_argument("--git-ref", type=str, default=None)
    parser.add_argument("--gpus", type=int, default=None)
    parser.add_argument("--nodes", type=int, default=None)
    parser.add_argument("--cpus", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-spot", action="store_true", help="Disable spot instances")
    parser.add_argument("--copies", type=int, default=1, help="Number of identical job copies to launch")
    parser.add_argument(
        "--timeout-hours",
        type=float,
        default=None,
        help="Automatically terminate the job after this many hours (supports decimals, e.g., 1.5 for 90 minutes)",
    )
    (args, cmd_args) = parser.parse_known_args()

    git_ref = args.git_ref
    if not git_ref:
        git_ref = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()

    task = sky.Task.from_yaml("./devops/skypilot/config/sk_train.yaml")
    task = task.update_envs(
        dict(
            METTA_RUN_ID=args.run,
            METTA_CMD=args.cmd,
            METTA_CMD_ARGS=" ".join(cmd_args),
            METTA_GIT_REF=git_ref,
        )
    )
    task.name = args.run
    task.validate_name()

    task = patch_task(
        task, cpus=args.cpus, gpus=args.gpus, nodes=args.nodes, no_spot=args.no_spot, timeout_hours=args.timeout_hours
    )

    if args.copies == 1:
        launch_task(task, dry_run=args.dry_run)
    else:
        for _ in range(1, args.copies + 1):
            copy_task = copy.deepcopy(task)
            run_id = args.run
            copy_task = copy_task.update_envs({"METTA_RUN_ID": run_id})
            copy_task.name = run_id
            copy_task.validate_name()
            launch_task(copy_task, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
