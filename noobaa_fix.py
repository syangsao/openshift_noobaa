#!/usr/bin/env python3
"""
noobaa_fix.py — Diagnose and fix NooBaa CrashLoopBackOff issues on OpenShift ODF.

Handles two known issues:
  1. Version mismatch between noobaa-core StatefulSet and backing store agents
  2. CNPG PostgreSQL replica stuck in pg_rewind loop (no common ancestor)

Configuration is loaded from (highest priority first):
  1. CLI flags (--ssh, --ssh-user, --ssh-key, --kubeconfig, --namespace)
  2. Environment variables (SSH_HOST, SSH_USER, SSH_KEY, KUBECONFIG, NAMESPACE)
  3. .env file next to the script or in the current working directory
  4. Defaults (see .env.example)

Usage:
  python3 noobaa_fix.py                  # With .env file
  python3 noobaa_fix.py --direct         # Direct cluster access
  python3 noobaa_fix.py --dry-run        # Show what would be done
  python3 noobaa_fix.py --check-only     # Diagnose without fixing
  python3 noobaa_fix.py --fix version-mismatch
  python3 noobaa_fix.py --fix pg-replica
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

NAMESPACE_DEFAULT = "openshift-storage"

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


# ── .env Loading ─────────────────────────────────────────────────────────────

def load_env(env_path: str) -> dict:
    """Load key=value pairs from a .env file."""
    env = {}
    path = Path(env_path)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            env[key.strip()] = value
    return env


def resolve_config(args: argparse.Namespace) -> tuple[dict, Path | None]:
    """Resolve configuration from .env file, env vars, and CLI flags.

    Priority (highest to lowest):
      1. CLI flags
      2. OS environment variables
      3. .env file
      4. Defaults
    """
    # Find .env file (next to script, or in cwd, or --env-file)
    script_dir = Path(__file__).resolve().parent
    env_file = None
    candidates = []
    if args.env_file:
        candidates.append(Path(args.env_file))
    candidates.extend([script_dir / ".env", Path.cwd() / ".env"])
    for candidate in candidates:
        if candidate.exists():
            env_file = candidate
            break

    env_values = load_env(str(env_file)) if env_file else {}

    def resolve(cli_val, env_key, default=""):
        """Resolve a config value with priority: CLI > env var > .env > default."""
        if cli_val is not None and cli_val != "":
            return cli_val
        if env_key in os.environ:
            return os.environ[env_key]
        if env_key in env_values:
            return env_values[env_key]
        return default

    config = {
        "SSH_HOST": resolve(args.ssh, "SSH_HOST", ""),
        "SSH_USER": resolve(args.ssh_user, "SSH_USER", ""),
        "SSH_KEY": resolve(args.ssh_key, "SSH_KEY", ""),
        "KUBECONFIG": resolve(args.kubeconfig, "KUBECONFIG", ""),
        "NAMESPACE": resolve(args.namespace, "NAMESPACE", NAMESPACE_DEFAULT),
    }

    return config, env_file


# ── oc Command Execution ─────────────────────────────────────────────────────

def run_oc(args: list[str], namespace: str, kubeconfig: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run an `oc` command, respecting KUBECONFIG."""
    env = None
    if kubeconfig:
        kc_path = Path(kubeconfig).expanduser()
        if kc_path.exists():
            env = {**os.environ, "KUBECONFIG": str(kc_path)}
    cmd = ["oc", "-n", namespace] + args
    return run_cmd(cmd, check=check, capture=True)


def run_oc_remote(
    ssh_host: str, ssh_user: str, ssh_key: str,
    oc_args: list[str], namespace: str, kubeconfig: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run an `oc` command via SSH through a jump host."""
    kc_path = Path(kubeconfig).expanduser() if kubeconfig else ""
    oc_args_str = " ".join(oc_args)
    full_cmd = (
        f"ssh -i {ssh_key} {ssh_user}@{ssh_host} "
        f"'KUBECONFIG={kc_path} oc -n {namespace} {oc_args_str}'"
    )
    return subprocess.run(
        full_cmd,
        shell=True,
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def oc(
    args: list[str],
    mode: str,
    ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Dispatch oc command based on connection mode."""
    if mode == "direct":
        return run_oc(args, namespace, kubeconfig, check)
    else:
        return run_oc_remote(ssh_host, ssh_user, ssh_key, args, namespace, kubeconfig, check)


# ── Issue 1: Version Mismatch ────────────────────────────────────────────────

def diagnose_version_mismatch(
    mode: str, ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
) -> dict:
    """Check if noobaa-core image version mismatches backing store agent images."""
    section("DIAGNOSIS: Backing Store Agent Version Mismatch")

    # Get noobaa-core image
    info("Checking noobaa-core StatefulSet image...")
    try:
        core_result = oc(
            ["get", "sts/noobaa-core", "-o", "jsonpath='{.spec.template.spec.containers[0].image}'"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
        )
        core_image = core_result.stdout.strip().strip("'")
        info(f"  noobaa-core image: {core_image}")
    except subprocess.CalledProcessError as e:
        error(f"  Failed to get noobaa-core image: {e.stderr.strip()}")
        return {"fixable": False, "reason": "Cannot read noobaa-core image"}

    # Get backing store agent image (try multiple approaches)
    info("Checking backing store agent images...")
    agent_image = ""

    # Try label selector
    try:
        agents_result = oc(
            ["get", "pods", "-l", "backingstore-name=noobaa-default-backing-store",
             "-o", "jsonpath='{.items[0].spec.containers[0].image}'"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
            check=False,
        )
        agent_image = agents_result.stdout.strip().strip("'") if agents_result.returncode == 0 else ""
    except Exception:
        agent_image = ""

    # Fallback: get all pods and find agent pods
    if not agent_image:
        info("  Falling back to listing all NooBaa-related pods...")
        try:
            pods_result = oc(
                ["get", "pods", "--no-headers"],
                mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
            )
            for line in pods_result.stdout.strip().split("\n"):
                if "noobaa-pod-" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pod_name = parts[0]
                        pod_result = oc(
                            ["get", "pod", pod_name, "-o", "jsonpath='{.spec.containers[0].image}'"],
                            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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
    crash_pods = []
    try:
        crash_pods_result = oc(
            ["get", "pods", "--no-headers", "-o", "custom-columns=NAME:.metadata.name,STATUS:.status.phase"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
        )
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


def fix_version_mismatch(
    diag: dict, dry_run: bool,
    mode: str, ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
):
    """Fix version mismatch by aligning noobaa-core image with agent image."""
    section("FIX: Align noobaa-core image with backing store agents")

    agent_image = diag["agent_image"]
    info(f"Target image: {agent_image}")

    if dry_run:
        info(f"[DRY-RUN] Would run: oc set image sts/noobaa-core -n {namespace} core={agent_image} noobaa-log-processor={agent_image}")
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
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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
    info(f"Verify with: oc get pods -n {namespace} | grep CrashLoopBackOff")
    return True


# ── Issue 2: CNPG PostgreSQL Replica Stuck ───────────────────────────────────

def diagnose_pg_replica(
    mode: str, ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
) -> dict:
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
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
            check=False,
        )
        if cluster_result.returncode == 0:
            status = cluster_result.stdout.strip().strip("'")
            info(f"  Cluster status: {status}")
            parts = status.split(",")
            if len(parts) >= 3:
                instances = int(parts[0]) if parts[0].isdigit() else 0
                ready = int(parts[1]) if parts[1].isdigit() else 0
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
    stuck_replica = None
    try:
        pods_result = oc(
            ["get", "pods", "-l", "cluster-name=noobaa-db-pg-cluster", "--no-headers"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
        )
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
                mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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


def fix_pg_replica(
    diag: dict, dry_run: bool,
    mode: str, ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
):
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
            result = oc(step["cmd"], mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig, check=False)
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
    info(f"  oc get pods -n {namespace} | grep db-pg")
    info(f"  oc get clusters.postgresql.cnpg.noobaa.io noobaa-db-pg-cluster -n {namespace}")
    return True


# ── Overall Status Check ─────────────────────────────────────────────────────

def check_overall_status(
    mode: str, ssh_host: str, ssh_user: str, ssh_key: str,
    namespace: str, kubeconfig: str,
):
    """Quick overview of NooBaa health."""
    section("NOOBAA OVERALL STATUS")

    # Pod summary
    info("Pod summary:")
    try:
        result = oc(
            ["get", "pods", "--no-headers"],
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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
            mode, ssh_host, ssh_user, ssh_key, namespace, kubeconfig,
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
        help="Connect directly (use local oc + KUBECONFIG)",
    )
    parser.add_argument(
        "--ssh",
        default=None,
        help="SSH jump host (overrides .env / SSH_HOST env var)",
    )
    parser.add_argument(
        "--ssh-user",
        default=None,
        help="SSH user (overrides .env / SSH_USER env var)",
    )
    parser.add_argument(
        "--ssh-key",
        default=None,
        help="SSH key path (overrides .env / SSH_KEY env var)",
    )
    parser.add_argument(
        "--kubeconfig",
        default=None,
        help="Path to kubeconfig (overrides .env / KUBECONFIG env var)",
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
        "--env-file",
        default=None,
        help="Path to .env file (default: .env next to script or in cwd)",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="OpenShift namespace (overrides .env / NAMESPACE env var)",
    )

    args = parser.parse_args()

    # Resolve config from .env, env vars, CLI flags
    config, env_file = resolve_config(args)

    if env_file:
        info(f"Loaded config from: {env_file}")

    # Determine connection mode
    if args.direct:
        mode = "direct"
    else:
        mode = "ssh"
        if not config["SSH_HOST"]:
            error("SSH_HOST is required. Set it in .env, SSH_HOST env var, or --ssh flag.")
            sys.exit(1)

    ssh_host = config["SSH_HOST"] if mode == "ssh" else ""
    ssh_user = config["SSH_USER"] if mode == "ssh" else ""
    ssh_key = config["SSH_KEY"] if mode == "ssh" else ""
    ns = config["NAMESPACE"]
    kubeconfig = config["KUBECONFIG"]

    print(color("\n╔══════════════════════════════════════════════════════════════╗", "BOLD"))
    print(color("║         NooBaa CrashLoopBackOff Diagnostic & Fix Tool       ║", "BOLD"))
    print(color("╚══════════════════════════════════════════════════════════════╝", "BOLD"))

    if args.dry_run:
        warn("[DRY-RUN MODE — no changes will be made]")
    if args.check_only:
        info("[CHECK-ONLY MODE — diagnostics only, no fixes]")

    # Overall status
    check_overall_status(mode, ssh_host, ssh_user, ssh_key, ns, kubeconfig)

    results = {}

    # Issue 1: Version mismatch
    if args.fix in ("version-mismatch", "all"):
        diag = diagnose_version_mismatch(mode, ssh_host, ssh_user, ssh_key, ns, kubeconfig)
        results["version_mismatch"] = diag
        if diag.get("fixable") and not args.check_only:
            fix_version_mismatch(diag, args.dry_run, mode, ssh_host, ssh_user, ssh_key, ns, kubeconfig)

    # Issue 2: PG replica
    if args.fix in ("pg-replica", "all"):
        diag = diagnose_pg_replica(mode, ssh_host, ssh_user, ssh_key, ns, kubeconfig)
        results["pg_replica"] = diag
        if diag.get("fixable") and not args.check_only:
            fix_pg_replica(diag, args.dry_run, mode, ssh_host, ssh_user, ssh_key, ns, kubeconfig)

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
