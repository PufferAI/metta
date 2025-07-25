import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import wandb

from metta.common.util.stats_client_cfg import get_stats_client_direct

logger = logging.getLogger(__name__)


@dataclass
class RunInfo:
    name: str
    id: str
    state: str
    created_at: datetime
    git_hash: Optional[str]
    artifacts: List[Dict[str, str]]


def get_recent_runs(
    entity: str,
    project: str,
    days_back: int = 30,
    limit: Optional[int] = None,
    run_name: Optional[str] = None,
    debug: bool = False,
) -> List[RunInfo]:
    """
    Fetch recent runs from W&B that are not cancelled or crashed.

    Args:
        entity: W&B entity name
        project: W&B project name
        days_back: Number of days to look back
        limit: Maximum number of runs to return
        run_name: Specific run name to fetch (if provided, ignores other filters)

    Returns:
        List of RunInfo objects with run details and artifacts
    """
    api = wandb.Api()

    # Calculate the date threshold
    date_threshold = datetime.now() - timedelta(days=days_back)

    # Build filters
    if run_name:
        # If specific run name provided, only filter by name
        filters = {"display_name": run_name}
    else:
        # Otherwise filter for finished runs in the time window
        filters = {
            "state": "finished",
            "created_at": {"$gt": date_threshold.isoformat()},
        }

    runs = api.runs(f"{entity}/{project}", filters=filters, order="-created_at")

    run_infos = []

    if debug:
        print(f"Debug: Fetching runs from {entity}/{project}")
        if run_name:
            print(f"Debug: Looking for specific run: {run_name}")
        else:
            print(f"Debug: Date threshold: {date_threshold}")

    for i, run in enumerate(runs):
        if limit and i >= limit:
            break

        # Extract git hash from run config or summary
        git_hash = None
        try:
            # Try different common locations for git hash
            git_hash = (
                run.config.get("git_hash")
                or run.config.get("git_commit")
                or run.config.get("git", {}).get("commit")
                or run.summary.get("git_hash")
                or run.summary.get("git_commit")
                or run.summary.get("_wandb", {}).get("git", {}).get("commit")
                or run.metadata.get("git", {}).get("commit")
                if hasattr(run, "metadata")
                else None
            )

            # Also check in the run's commit field directly
            if not git_hash and hasattr(run, "commit"):
                git_hash = run.commit
        except Exception:
            pass

        # Get artifacts for this run
        artifacts_data = []
        try:
            for artifact in run.logged_artifacts():
                artifacts_data.append(
                    {
                        "name": artifact.name,
                        "type": artifact.type,
                        "version": artifact.version,
                        "size": artifact.size,
                        "created_at": artifact.created_at,
                        "url": f"wandb://{run.entity}/{run.project}/{artifact.name}:{artifact.version}",
                    }
                )
        except Exception as e:
            print(f"Warning: Could not fetch artifacts for run {run.name}: {e}")

        run_info = RunInfo(
            name=run.name,
            id=run.id,
            state=run.state,
            created_at=datetime.fromisoformat(run.created_at),
            git_hash=git_hash,
            artifacts=artifacts_data,
        )

        run_infos.append(run_info)

    # Sort by created_at in descending order (most recent first)
    run_infos.sort(key=lambda x: x.created_at, reverse=True)

    return run_infos


def post_policies_to_stats(runs: List[RunInfo], stats_db_uri: str):
    """Post model artifacts as policies to the stats database."""
    if not stats_db_uri:
        print("\nNo STATS_DB_URI provided, skipping policy posting.")
        return

    print(f"\nConnecting to stats database at {stats_db_uri}...")

    # Create HTTP client and StatsClient
    stats_client = get_stats_client_direct(stats_db_uri, logger)
    if not stats_client:
        print("No stats client")
        return

    # Validate authentication
    stats_client.validate_authenticated()
    print("Successfully authenticated with stats server.")

    # Collect all model artifacts
    policies_to_create = []
    for run in runs:
        for artifact in run.artifacts:
            if artifact["type"] == "model":
                policy_name = artifact["name"]
                policy_url = artifact["url"]

                policies_to_create.append(
                    {"name": policy_name, "url": policy_url, "run_name": run.name, "git_hash": run.git_hash}
                )

    if not policies_to_create:
        print("No model artifacts found to post as policies.")
        return

    # Get existing policy names
    policy_names = [p["name"] for p in policies_to_create]
    existing_policies = stats_client.get_policy_ids(policy_names)
    existing_names = set(existing_policies.policy_ids.keys())

    # Post new policies
    posted_count = 0
    for policy_info in policies_to_create:
        if policy_info["name"] not in existing_names:
            try:
                description = f"From run: {policy_info['run_name']}"
                if policy_info["git_hash"]:
                    description += f" (git: {policy_info['git_hash'][:8]})"

                response = stats_client.create_policy(
                    name=policy_info["name"], description=description, url=policy_info["url"]
                )
                print(f"  Posted policy: {policy_info['name']} (ID: {response.id})")
                posted_count += 1
            except Exception as e:
                print(f"  Error posting policy {policy_info['name']}: {e}")
        else:
            print(f"  Policy already exists: {policy_info['name']}")

    print(f"\nPosted {posted_count} new policies to stats database.")


def print_runs_with_artifacts(runs: List[RunInfo], run_name: Optional[str] = None):
    """Print runs and their artifacts in a formatted way."""
    if not runs:
        if run_name:
            print(f"No run found with name: {run_name}")
        else:
            print("No runs found matching the criteria.")
        return

    if run_name:
        print(f"\nFound run '{run_name}':\n")
    else:
        print(f"\nFound {len(runs)} recent successful runs:\n")
    print("=" * 80)

    for run in runs:
        print(f"\nRun: {run.name}")
        print(f"  ID: {run.id}")
        print(f"  State: {run.state}")
        print(f"  Created: {run.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        if run.git_hash:
            print(f"  Git Hash: {run.git_hash}")

        if run.artifacts:
            print(f"  Artifacts ({len(run.artifacts)}):")

            # Group artifacts by type
            artifacts_by_type = {}
            for artifact in run.artifacts:
                artifact_type = artifact["type"]
                if artifact_type not in artifacts_by_type:
                    artifacts_by_type[artifact_type] = []
                artifacts_by_type[artifact_type].append(artifact)

            # Print artifacts grouped by type
            for artifact_type, type_artifacts in artifacts_by_type.items():
                print(f"    {artifact_type}:")
                for artifact in sorted(type_artifacts, key=lambda x: x["name"]):
                    size_mb = artifact["size"] / (1024 * 1024) if artifact["size"] else 0
                    print(f"      - {artifact['name']} (v{artifact['version']}, {size_mb:.1f} MB)")
        else:
            print("  No artifacts")

        print("-" * 80)
