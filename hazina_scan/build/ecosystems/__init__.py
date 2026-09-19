"""Turn one discovered project into the phase-by-phase command plan hazina-scan runs it with.

Each sibling module answers one ecosystem's version of the same five questions -- how to
resolve and install dependencies, how to build, how to list the tests, how to run them, and
how to read coverage back afterwards -- as plain argument lists a caller can hand straight to
`hazina_scan.env.run()`. `plan_for()` is the one entry point that matters to the rest of the
build check: given a discovered project and the runtime this host resolved for it, it picks
the right module, folds the runtime's environment overlay into the child environment every
command in the plan will run under, and hands back one dict.
"""

from __future__ import annotations

from pathlib import Path

from .. import runtime
from . import dotnet, go, jvm, node, php, python, ruby, rust

__all__ = ["PLANNERS", "ECOSYSTEM_LANE", "plan_for"]

#: Ecosystem name -> the module whose `plan()` builds that ecosystem's commands. Maven and
#: Gradle share one module because both answer to the JVM toolchain and differ only in which
#: build tool and wrapper script they reach for.
PLANNERS = {
    "node": node.plan,
    "python": python.plan,
    "go": go.plan,
    "rust": rust.plan,
    "maven": jvm.plan,
    "gradle": jvm.plan,
    "dotnet": dotnet.plan,
    "ruby": ruby.plan,
    "php": php.plan,
}

#: Ecosystem name -> the one runtime lane its commands actually run on, so runtime
#: resolution only pays for the lane a project needs rather than enumerating every
#: interpreter and JDK on the host for every project. PHP is left out on purpose: nothing
#: this tool reads from a PHP project names a PHP version, and inventing a source to read
#: would only be pretending at a taxonomy PHP projects do not follow here.
ECOSYSTEM_LANE = {
    "node": "node",
    "python": "python",
    "rust": "rust",
    "go": "go",
    "maven": "java",
    "gradle": "java",
    "dotnet": "dotnet",
    "ruby": "ruby",
}


def plan_for(
    project,
    scratch: Path,
    env: dict,
    timeout: int,
    restore: list[tuple[Path, int]],
    runtime_plan: runtime.Plan,
) -> dict:
    """The full plan for one project: which module built it, under which environment.

    Runtime resolution has already happened by the time this is called -- `runtime_plan` is
    its result -- and the overlay it carries is applied to `env` before any planner runs, so
    a Python plan bakes the resolved interpreter's own path into every phase and a Maven plan
    sees `JAVA_HOME` set before it even looks for `mvn`. The chosen planner and the resulting
    environment are recorded on the returned plan under `"runtime"` and `"env"` so a caller
    downstream never has to re-derive either.
    """
    env = runtime.apply_overlay(env, runtime_plan.overlay)
    planner = PLANNERS.get(project.ecosystem)
    if planner is None:
        plan = {
            "toolchain": project.ecosystem,
            "tool": None,
            "preflight": f"command not found: {project.ecosystem}",
        }
    elif project.ecosystem == "python":
        plan = planner(
            project,
            scratch,
            env,
            timeout,
            restore,
            base_python=runtime_plan.interpreter.get("python"),
        )
    else:
        plan = planner(project, scratch, env, timeout, restore)
    plan["runtime"] = runtime_plan
    plan["env"] = env
    return plan
