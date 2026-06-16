#!/usr/bin/env python3
"""
noobaa_fix.py — Diagnose and fix NooBaa CrashLoopBackOff issues on OpenShift ODF.

Handles two known issues:
  1. Version mismatch between noobaa-core StatefulSet and backing store agents
  2. CNPG PostgreSQL replica stuck in pg_rewind loop (no common ancestor)

Usage:
  # Run all checks and fixes:
  python3 noobaa_fix.py

  # SSH through jump host (default):
  python3 noobaa_fix.py --ssh grogu.syangsao.lab --ssh-user syangsao --ssh-key ~/.ssh/id_grogu

  # Direct cluster access (kubeconfig already set):
  python3 noobaa_fix.py --direct

  # Dry run (show what would be done):
  python3 noobaa_fix.py --dry-run

  # Fix only a specific issue:
  python3 noobaa_fix.py --fix version-mismatch
  python3 noobaa_fix.py --fix pg-replica

Environment:
  KUBECONFIG  — path to kubeconfig (default: ~/ocp421-luke/auth/kubeconfig on grogu)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

NAMESPACE = "openshift-storage"
SSH_KEY_DEFAULT = "~/.ssh/id_grogu"
SSH_USER_DEFAULT = "syangsao"
KUBECONFIG_DEFAULT = "~/ocp421-luke/auth/kubeconfig"

COLORS = {
    "RED": "\033[91m",
    "GREEN": "\033[92m",
    "YELLOW": "\033[93m",
    "CYAN": "\033[96m",
    "BOLD": "\033[1m",
    "RESET": "\033[0m",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def color(text: str, color: str) -> str:
    return f"{COLORS[color]}{text}{COLORS['RESET']}"


def info(msg: str):
    print(color(f"ℹ  {msg}", "CYAN"))


def success(msg: str):
    print(color(f"✓  {msg}", "GREEN"))


def warn(msg: str):
    print(color(f"⚠  {msg}", "YELLOW"))


def error(msg: str):
    print(color(f"✗  {msg}", "RED"))


def section(title: str):
    print()
    print(color(f"{'━' * 60}", "BOLD"))
    print(color(f"  {title}", "BOLD"))
    print(color(f"{'━' * 60}", "BOLD"))


def run_cmd(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a local command and return the result."""
    if capture:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )
    else:
        return subprocess.run(cmd, check=check, timeout=60)


def run_oc(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run an `oc` command, respecting KUBECONFIG."""
    env = None
    kubeconfig = Path(KUBECONFIG_DEFAULT).expanduser()
    if kubeconfig.exists():
        import os
        env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    cmd = ["oc", "-n", NAMESPACE] + args
    return run_cmd(cmd, check=check)


def run_oc_remote(ssh_host: str, ssh_user: str, ssh_key: str, oc_args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run an `oc` command via SSH through a jump host."""
    kubeconfig = Path(KUBECONFIG_DEFAULT).expanduser()
    oc_args_str = " ".join(oc_args)
    full_cmd = (
        f"ssh -i {ssh_key} {ssh_user}@{ssh_host} "
        f"'KUBECONFIG={kubeconfig} oc -n {NAMESPACE} {oc_args_str}'"
    )
    return subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def oc(args: list[str], mode: str, ssh_host: str, ssh_user: str, ssh_key: str, check: bool = True) -> subprocess.CompletedProcess:
    """Dispatch oc command based on connection mode."""
    if mode == "direct":
        return run_oc(args, check)
    else:
        return run_oc_remote(ssh_host, ssh_user, ssh_key, args, check)


def oc_get_json(args: list[str], mode: str, ssh_host: str, ssh_user: str, ssh_key: str) -> dict:
    """Run oc get ... -o json and parse the result."""
    result = oc(args + ["-o", "json"], mode, ssh_host, ssh_user, ssh_key)
    return json.loads(result.stdout)


def oc_get_field(args: list[str], mode: str, ssh_host: str, ssh_user: str, ssh_key: str) -> str:
    """Run oc get ... with jsonpath and return the string value."""
    result = oc(args + ["-o", "jsonpath='{.spec.template.spec.containers[0].image}'"], mode, ssh_host, ssh_user, ssh_key)
    return result.stdout.strip().strip("'")


# ── Issue 1: Version Mismatch ────────────────────────────────────────────────

def diagnose_version_mismatch(mode: str, ssh_host: str, ssh_user: str, ssh_key: str) -> dict:
    """Check if noobaa-core image version mismatches backing store agent images."""
    section("DIAGNOSIS: Backing Store Agent Version Mismatch")

    # Get noobaa-core image
    info("Checking noobaa-core StatefulSet image...")
    try:
        core_result = oc(
            ["get", "sts/noobaa-core", "-o", "jsonpath='{.spec.template.spec.containers[0].image}'"],
            mode, ssh_host, ssh_user, ssh_key,
        )
        core_image = core_result.stdout.strip().strip("'")
        info(f"  noobaa-core image: {core_image}")
    except subprocess.CalledProcessError as e:
        error(f"  Failed to get noobaa-core image: {e.stderr.strip()}")
        return {"fixable": False, "reason": "Cannot read noobaa-core image"}

    # Get backing store agent image (first one found)
    info("Checking backing store agent images...")
    try:
        agents_result = oc(
            ["get", "pods", "-l", " backingstore-name=noobaa-default-backing-store",
             "-o", "jsonpath='{.items[0].spec.containers[0].image}'"],
            mode, ssh_host, ssh_user, ssh_key,
            check=False,
        )
        agent_image = agents_result.stdout.strip().strip("'") if agents_result.returncode == 0 else ""
    except Exception:
        agent_image = ""

    # Also try by pod name pattern
    if not agent_image or agent_image == "":
        info("  Trying alternative agent pod query...")
        try:
            agents_result = oc(
                ["get", "pods", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image",
                 "-l", " backingstore-name=noobaa-default-backing-store"],
                mode, ssh_host, ssh_user, ssh_key,
                check=False,
            )
            if agents_result.returncode == 0 and agents_result.stdout.strip():
                lines = agents_result.stdout.strip().split("\n")
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 2:
                        agent_image = parts[-1]
                        break
        except Exception:
            pass

    # Fallback: get all pods and find agent pods
    if not agent_image or agent_image == "":
        info("  Falling back to listing all NooBaa-related pods...")
        try:
            pods_result = oc(
                ["get", "pods", "--no-headers"],
                mode, ssh_host, ssh_user, ssh_key,
            )
            agent_image = ""
            for line in pods_result.stdout.strip().split("\n"):
                if "noobaa-pod-" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pod_name = parts[0]
                        # Get image from specific pod
                        pod_result = oc(
                            ["get", "pod", pod_name, "-o", "jsonpath='{.spec.containers[0].image}'"],
                            mode, ssh_host, ssh_user, ssh_key,
                            check=False,
                        )
                        if pod_result.returncode == 0:
                            agent_image = pod_result.stdout.strip().strip("'")
                            info(f"  Found agent pod {pod_name}: {agent_image}")
                            break
        except Exception:
            pass

    if not agent_image:
        warn("  Could not determine backing store agent image")
        return {"fixable": False, "reason": "Cannot read agent image"}

    info(f"  Agent image: {agent_image}")

    # Extract versions
    core_version = core_image.split("/")[-1].split("@")[0] if "/" in core_image else core_image.split("@")[0]
    agent_version = agent_image.split("/")[-1].split("@")[0] if "/" in agent_image else agent_image.split("@")[0]

    info(f"  Core version:  {core_version}")
    info(f"  Agent version: {agent_version}")

    # Check for CrashLoopBackOff pods
    info("Checking for CrashLoopBackOff pods...")
    try:
        crash_pods_result = oc(
            ["get", "pods", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase"],
            mode, ssh_host, ssh_user, ssh_key,
        )
        crash_pods = []
        for line in crash_pods_result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and "CrashLoopBackOff" in parts[-1]:
                crash_pods.append(parts[0])

        if crash_pods:
            warn(f"  Found {len(crash_pods)} CrashLoopBackOff pod(s):")
            for pod in crash_pods:
                print(f"    - {pod}")
        else:
            success("  No CrashLoopBackOff pods found")
    except Exception as e:
        warn(f"  Could not check for CrashLoopBackOff pods: {e}")

    # Compare versions
    if core_version != agent_version:
        error(f"  VERSION MISMATCH DETECTED!")
        error(f"  Core ({core_version}) != Agent ({agent_version})")
        return {
            "fixable": True,
            "core_image": core_image,
            "agent_image": agent_image,
            "core_version": core_version,
            "agent_version": agent_version,
            "crash_pods": crash_pods,
        }
    else:
        success("  Versions match — no version mismatch issue")
        return {
            "fixable": False,
            "reason": "Versions match",
            "core_image": core_image,
            "agent_image": agent_image,
            "crash_pods": crash_pods,
        }


def fix_version_mismatch(diag: dict, dry_run: bool, mode: str, ssh_host: str, ssh_user: str, ssh_key: str):
    """Fix version mismatch by aligning noobaa-core image with agent image."""
    section("FIX: Align noobaa-core image with backing store agents")

    agent_image = diag["agent_image"]
    info(f"Target image: {agent_image}")

    cmd_desc = (
        f"oc set image sts/noobaa-core -n {NAMESPACE} "
        f"core={agent_image} noobaa-log-processor={agent_image}"
    )

    if dry_run:
        info(f"[DRY-RUN] Would run: {cmd_desc}")
        return True

    # Execute the fix
    info("Patching noobaa-core StatefulSet...")
    try:
        result = oc(
            [
                "set", "image", "sts/noobaa-core",
                f"core={agent_image}",
                f"noobaa-log-processor={agent_image}",
            ],
            mode, ssh_host, ssh_user, ssh_key,
        )
        success("Image update applied:")
        print(f"  {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        error(f"Failed to patch image: {e.stderr.strip()}")
        return False

    # Verify
    info("Verifying fix...")
    import time
    time.sleep(5)
    try:
        verify_result = oc(
            ["get", "sts/noobaa-core", "-o", "jsonpath='{.spec.template.spec.containers[0].image}'"],
            mode, ssh_host, ssh_user, ssh_key,
        )
        new_image = verify_result.stdout.strip().strip("'")
        new_version = new_image.split("/")[-1].split("@")[0] if "/" in new_image else new_image.split("@")[0]
        if new_version == diag["agent_version"]:
            success(f"noobaa-core is now running version {new_version}")
        else:
            warn(f"noobaa-core image is {new_version} (expected {diag['agent_version']})")
    except Exception as e:
        warn(f"Could not verify: {e}")

    info("Backing store agents should reconnect and stop crashing within 1-2 minutes.")
    info("Verify with: oc get pods -n openshift-storage | grep CrashLoopBackOff")
    return True


# ── Issue 2: CNPG PostgreSQL Replica Stuck ───────────────────────────────────

def diagnose_pg_replica(mode: str, ssh_host: str, ssh_user: str, ssh_key: str) -> dict:
    """Check if CNPG PostgreSQL replica is stuck in pg_rewind loop."""
    section("DIAGNOSIS: CNPG PostgreSQL Replica Status")

    # Check CNPG cluster status
    info("Checking CNPG cluster status...")
    try:
        cluster_result = oc(
            [
                "get", "clusters.postgresql.cnpg.noobaa.io", "noobaa-db-pg-cluster",
                "-o", "jsonpath='{.status.instances},{.status.readyInstances},{.status.phase}'"
            ],
            mode, ssh_host, ssh_user, ssh_key,
            check=False,
        )
        if cluster_result.returncode == 0:
            status = cluster_result.stdout.strip().strip("'")
            info(f"  Cluster status: {status}")
            parts = status.split(",")
            if len(parts) >= 3:
                instances = int(parts[0]) if parts[0].isdigit() else 0
                ready = int(parts[1]) if parts[1].isdigit() else 0
                phase = parts[2]
                if ready < instances:
                    warn(f"  Only {ready}/{instances} instances ready")
                else:
                    success(f"  All {ready}/{instances} instances ready")
        else:
            warn("  Could not get CNPG cluster status")
    except Exception as e:
        warn(f"  Could not get CNPG cluster status: {e}")

    # Check db-pg pods
    info("Checking db-pg pod status...")
    try:
        pods_result = oc(
            ["get", "pods", "-l", "cluster-name=noobaa-db-pg-cluster", "--no-headers"],
            mode, ssh_host, ssh_user, ssh_key,
        )
        stuck_replica = None
        for line in pods_result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                ready = parts[1]
                status = parts[2]
                info(f"  {name}: {ready} {status}")
                if "0/1" in ready and status == "Running":
                    stuck_replica = name
                    warn(f"  Replica {name} is stuck (0/1 Running)")
                elif status == "CrashLoopBackOff":
                    stuck_replica = name
                    error(f"  Replica {name} is CrashLoopBackOff")
    except subprocess.CalledProcessError as e:
        error(f"  Failed to get pod status: {e.stderr.strip()}")
        return {"fixable": False, "reason": "Cannot read pod status"}

    if stuck_replica:
        # Check logs for pg_rewind error
        info(f"Checking logs for pg_rewind errors on {stuck_replica}...")
        try:
            logs_result = oc(
                ["logs", stuck_replica, "--tail=100"],
                mode, ssh_host, ssh_user, ssh_key,
                check=False,
            )
            if logs_result.returncode == 0:
                log_text = logs_result.stdout
                if "pg_rewind" in log_text and "common ancestor" in log_text:
                    error("  pg_rewind 'no common ancestor' error detected!")
                    return {
                        "fixable": True,
                        "stuck_replica": stuck_replica,
                        "issue": "pg_rewind_no_common_ancestor",
                    }
                elif "pg_rewind" in log_text:
                    warn("  pg_rewind errors detected (different issue)")
                    return {
                        "fixable": False,
                        "reason": "pg_rewind error but not 'no common ancestor'",
                        "stuck_replica": stuck_replica,
                    }
                elif "PG_VERSION" in log_text and "No such file" in log_text:
                    error("  Missing PG_VERSION — pgdata is empty")
                    return {
                        "fixable": True,
                        "stuck_replica": stuck_replica,
                        "issue": "empty_pgdata",
                    }
                else:
                    warn("  Could not identify specific pg_rewind issue from logs")
                    # Show last few log lines for manual inspection
                    last_lines = [l for l in log_text.strip().split("\n")[-10:] if l.strip()]
                    if last_lines:
                        info("  Last 10 log lines:")
                        for line in last_lines:
                            print(f"    {line}")
        except Exception as e:
            warn(f"  Could not read logs: {e}")

        return {
            "fixable": False,
            "reason": "Replica stuck but root cause unclear",
            "stuck_replica": stuck_replica,
        }

    success("  No stuck PostgreSQL replicas detected")
    return {"fixable": False, "reason": "No stuck replicas"}


def fix_pg_replica(diag: dict, dry_run: bool, mode: str, ssh_host: str, ssh_user: str, ssh_key: str):
    """Fix stuck CNPG replica by performing manual pg_basebackup."""
    replica = diag["stuck_replica"]
    section(f"FIX: Resync {replica} via pg_basebackup")

    if dry_run:
        info(f"[DRY-RUN] Would perform these steps on {replica}:")
        info(f"  1. Clear pgdata on {replica}")
        info(f"  2. Remove config files")
        info(f"  3. Run pg_basebackup from noobaa-db-pg-cluster-rw")
        info(f"  4. Delete pod so CNPG restarts with fresh pgdata")
        return True

    steps = [
        {
            "name": "Clear pgdata on broken replica",
            "cmd": [
                "exec", replica, "--",
                "find", "/var/lib/postgresql/data/pgdata/", "-mindepth", "1", "-delete",
            ],
        },
        {
            "name": "Remove CNPG config files",
            "cmd": [
                "exec", replica, "--",
                "rm", "-f",
                "/var/lib/postgresql/data/pgdata/pg_hba.conf",
                "/var/lib/postgresql/data/pgdata/pg_ident.conf",
            ],
        },
        {
            "name": "Run pg_basebackup from primary",
            "cmd": [
                "exec", replica, "--", "bash", "-c",
                "export PGSSLKEY=/controller/certificates/streaming_replica.key "
                "export PGSSLCERT=/controller/certificates/streaming_replica.crt "
                "export PGSSLROOTCERT=/controller/certificates/server-ca.crt "
                "export PGSSLMODE=verify-ca "
                "pg_basebackup -h noobaa-db-pg-cluster-rw "
                "-U streaming_replica "
                "-D /var/lib/postgresql/data/pgdata "
                "-X stream -P",
            ],
        },
        {
            "name": "Delete pod for CNPG restart",
            "cmd": ["delete", "pod", replica, "--grace-period=30"],
        },
    ]

    for i, step in enumerate(steps, 1):
        info(f"Step {i}/{len(steps)}: {step['name']}")
        try:
            result = oc(step["cmd"], mode, ssh_host, ssh_user, ssh_key, check=False)
            if result.returncode == 0:
                success(f"  Completed: {step['name']}")
                if result.stdout.strip():
                    print(f"  Output: {result.stdout.strip()[:200]}")
            else:
                error(f"  Failed: {step['name']}")
                if result.stderr.strip():
                    print(f"  Error: {result.stderr.strip()[:300]}")
                return False
        except subprocess.TimeoutExpired:
            # pg_basebackup may take a while — allow it
            if i == 3:
                success(f"  Completed (with timeout): {step['name']}")
            else:
                error(f"  Timed out: {step['name']}")
                return False
        except Exception as e:
            error(f"  Exception in step {step['name']}: {e}")
            return False

    success(f"{replica} fix complete!")
    info("CNPG will restart the pod with fresh pgdata.")
    info("The instance may be promoted as new primary — this is normal.")
    info("Verify with:")
    info(f"  oc get pods -n {NAMESPACE} | grep db-pg")
    info(f"  oc get clusters.postgresql.cnpg.noobaa.io noobaa-db-pg-cluster -n {NAMESPACE}")
    return True


# ── Overall Status Check ─────────────────────────────────────────────────────

def check_overall_status(mode: str, ssh_host: str, ssh_user: str, ssh_key: str):
    """Quick overview of NooBaa health."""
    section("NOOBAA OVERALL STATUS")

    # Pod summary
    info("Pod summary:")
    try:
        result = oc(
            ["get", "pods", "--no-headers"],
            mode, ssh_host, ssh_user, ssh_key,
        )
        status_counts = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 3:
                status = parts[2]
                status_counts[status] = status_counts.get(status, 0) + 1

        for status, count in sorted(status_counts.items()):
            if status in ("CrashLoopBackOff", "Error", "Evicted"):
                error(f"  {status}: {count}")
            elif status == "Running":
                success(f"  {status}: {count}")
            else:
                warn(f"  {status}: {count}")
    except Exception as e:
        error(f"  Could not get pod status: {e}")

    # NooBaa CR status
    info("NooBaa CR status:")
    try:
        result = oc(
            ["get", "noobaa", "noobaa", "-o", "jsonpath='{.status.phase}'"],
            mode, ssh_host, ssh_user, ssh_key,
            check=False,
        )
        if result.returncode == 0:
            phase = result.stdout.strip().strip("'")
            if phase == "Ready":
                success(f"  NooBaa phase: {phase}")
            else:
                warn(f"  NooBaa phase: {phase}")
    except Exception as e:
        warn(f"  Could not get NooBaa CR status: {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Diagnose and fix NooBaa CrashLoopBackOff issues on OpenShift ODF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Connect directly (use local oc + KUBECONFIG env var)",
    )
    parser.add_argument(
        "--ssh",
        default=None,
        help="SSH jump host (default: grogu.syangsao.lab)",
    )
    parser.add_argument(
        "--ssh-user",
        default=SSH_USER_DEFAULT,
        help=f"SSH user (default: {SSH_USER_DEFAULT})",
    )
    parser.add_argument(
        "--ssh-key",
        default=SSH_KEY_DEFAULT,
        help=f"SSH key path (default: {SSH_KEY_DEFAULT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--fix",
        choices=["version-mismatch", "pg-replica", "all"],
        default="all",
        help="Which issue to fix (default: all)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only diagnose, don't apply fixes",
    )
    parser.add_argument(
        "--namespace",
        default=NAMESPACE,
        help=f"OpenShift namespace (default: {NAMESPACE})",
    )

    args = parser.parse_args()

    # Determine connection mode
    if args.direct:
        mode = "direct"
        ssh_host = ssh_user = ssh_key = ""
    else:
        mode = "ssh"
        ssh_host = args.ssh or "grogu.syangsao.lab"
        ssh_user = args.ssh_user
        ssh_key = args.ssh_key

    ns = args.namespace

    print(color("\n╔══════════════════════════════════════════════════════════════╗", "BOLD"))
    print(color("║         NooBaa CrashLoopBackOff Diagnostic & Fix Tool       ║", "BOLD"))
    print(color("╚══════════════════════════════════════════════════════════════╝", "BOLD"))

    if args.dry_run:
        warn("[DRY-RUN MODE — no changes will be made]")
    if args.check_only:
        info("[CHECK-ONLY MODE — diagnostics only, no fixes]")

    # Overall status
    check_overall_status(mode, ssh_host, ssh_user, ssh_key)

    results = {}

    # Issue 1: Version mismatch
    if args.fix in ("version-mismatch", "all"):
        diag = diagnose_version_mismatch(mode, ssh_host, ssh_user, ssh_key)
        results["version_mismatch"] = diag
        if diag.get("fixable") and not args.check_only:
            fix_version_mismatch(diag, args.dry_run, mode, ssh_host, ssh_user, ssh_key)

    # Issue 2: PG replica
    if args.fix in ("pg-replica", "all"):
        diag = diagnose_pg_replica(mode, ssh_host, ssh_user, ssh_key)
        results["pg_replica"] = diag
        if diag.get("fixable") and not args.check_only:
            fix_pg_replica(diag, args.dry_run, mode, ssh_host, ssh_user, ssh_key)

    # Summary
    section("SUMMARY")
    for issue, diag in results.items():
        if diag.get("fixable"):
            error(f"  {issue}: ISSUE DETECTED — {'FIXED' if not args.check_only and not args.dry_run else 'NEEDS FIX'}")
        else:
            reason = diag.get("reason", "unknown")
            success(f"  {issue}: OK ({reason})")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
