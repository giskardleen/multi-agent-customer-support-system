"""
agentcore_cli.py
================
Pre-written helper around the **AgentCore CLI** (`agentcore`, npm package
`@aws/agentcore`, https://github.com/aws/agentcore-cli) - do not modify.

The CLI is the deployment tool for Amazon Bedrock AgentCore. It replaces the
former Python `bedrock-agentcore-starter-toolkit`; both installed the same
`agentcore` command, so the old toolkit must be uninstalled
(`pip uninstall bedrock-agentcore-starter-toolkit`).

How the project uses it
-----------------------
* `agentcore/agentcore.json`  - declarative description of the AgentCore
  Runtime (name, entry point, code location, network mode, protocol,
  environment variables, execution role). The deploy pipeline updates the
  runtime entry in this file with the values the student passes in
  (`configure_runtime`).
* `build/runtime/`            - the code the CLI packages: this project's
  `src/*.py` modules, `config.py` and a `pyproject.toml` listing the runtime
  dependencies (`stage_runtime_code`). The CLI downloads matching arm64 /
  Python 3.12 wheels with `uv`, zips everything and uploads it - AgentCore
  *direct code deployment* (`build: CodeZip`).
* `agentcore deploy -y`       - synthesizes a CDK stack
  (`AgentCore-<project>-default`) that creates or updates the runtime
  (`deploy`). The first run also bootstraps CDK in the account
  (`CDKToolkit` stack), which takes a couple of minutes.
* `agentcore/.cli/deployed-state.json` - written by the CLI after a deploy;
  the runtime ARN is read from there (`deployed_runtime_arn`).

Prerequisites (see README): Node.js 20+, `npm install -g @aws/agentcore@0.30.0`,
`uv`, and AWS credentials for the project region.
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

# ─────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────
SRC_DIR       = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT  = os.path.dirname(SRC_DIR)                       # starter/
AGENTCORE_DIR = os.path.join(PROJECT_ROOT, 'agentcore')        # CLI project config
CONFIG_PATH   = os.path.join(AGENTCORE_DIR, 'agentcore.json')
TARGETS_PATH  = os.path.join(AGENTCORE_DIR, 'aws-targets.json')
STATE_PATH    = os.path.join(AGENTCORE_DIR, '.cli', 'deployed-state.json')
RUNTIME_CODE_DIR = os.path.join(PROJECT_ROOT, 'build', 'runtime')   # codeLocation

# ─────────────────────────────────────────────────────
# RUNTIME PACKAGE SETTINGS
# ─────────────────────────────────────────────────────
RUNTIME_ENTRYPOINT = 'agent_orchestrator.py'   # started by the runtime with no arguments -> serve mode
RUNTIME_PYTHON     = 'PYTHON_3_12'             # agentcore.json runtimeVersion
RUNTIME_MARKER     = '.agentcore-runtime'      # tells agent_orchestrator.__main__ to serve HTTP
RUNTIME_REQUIREMENTS = [
    'strands-agents>=1.0',
    'bedrock-agentcore>=0.1',
    'boto3>=1.42',
    'python-dotenv>=1.0',
]
RUNTIME_PROJECT_FILES = [
    os.path.join(SRC_DIR, 'agent_orchestrator.py'),
    os.path.join(SRC_DIR, 'agent_utils.py'),
    os.path.join(SRC_DIR, 'agent_observability.py'),
    os.path.join(SRC_DIR, 'bedrock_kb_retrieval.py'),
    os.path.join(SRC_DIR, 'agentcore_cli.py'),
    os.path.join(PROJECT_ROOT, 'config.py'),
]

INSTALL_HINT = (
    "The AgentCore CLI is not installed or not on PATH.\n"
    "  Install Node.js 20+ and uv, then run:  npm install -g @aws/agentcore@0.30.0\n"
    "  (uninstall the old toolkit first if present: "
    "pip uninstall bedrock-agentcore-starter-toolkit)\n"
    "  Docs: https://github.com/aws/agentcore-cli"
)


# ─────────────────────────────────────────────────────
# CLI PROCESS
# ─────────────────────────────────────────────────────

def cli_path() -> str:
    """Return the path of the `agentcore` executable or raise with install instructions."""
    path = shutil.which('agentcore')
    if not path:
        raise RuntimeError(INSTALL_HINT)
    return path


def cli_version() -> str:
    """Return the installed CLI version string (e.g. '0.30.0')."""
    out = subprocess.run([cli_path(), '--version'], capture_output=True, text=True)
    return (out.stdout or out.stderr).strip()


def run(*args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """
    Run `agentcore <args>` from the project root with the project region.
    Output streams to the terminal unless capture=True.
    """
    env = dict(os.environ)
    env.setdefault('AWS_REGION', config.AWS_REGION)
    env.setdefault('AWS_DEFAULT_REGION', config.AWS_REGION)
    result = subprocess.run(
        [cli_path(), *args], cwd=PROJECT_ROOT, env=env,
        capture_output=capture, text=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip() if capture else ''
        raise RuntimeError(f"agentcore {' '.join(args)} failed (exit {result.returncode})"
                           + (f":\n{detail}" if detail else ''))
    return result


# ─────────────────────────────────────────────────────
# agentcore.json
# ─────────────────────────────────────────────────────

def read_project_config() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as fh:
        return json.load(fh)


def write_project_config(spec: dict) -> None:
    with open(CONFIG_PATH, 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, indent=2)
        fh.write('\n')


def runtime_spec(spec: dict) -> dict:
    """Return the runtime entry for this project's agent, creating it if missing."""
    for rt in spec.setdefault('runtimes', []):
        if rt.get('name') == config.AGENTCORE_AGENT_NAME:
            return rt
    rt = {'name': config.AGENTCORE_AGENT_NAME, 'build': 'CodeZip'}
    spec['runtimes'].append(rt)
    return rt


def runtime_env_vars() -> dict:
    """Environment variables currently declared for the runtime in agentcore.json."""
    try:
        rt = runtime_spec(read_project_config())
    except FileNotFoundError:
        return {}
    return {e['name']: e['value'] for e in rt.get('envVars', []) if 'name' in e}


def configure_runtime(env_vars: dict = None, network_mode: str = None, protocol: str = None,
                      execution_role_arn: str = None) -> dict:
    """
    Update the runtime entry in agentcore/agentcore.json.

      env_vars           - merged into the runtime's `envVars` (existing keys are
                           overwritten, empty values are skipped)
      network_mode       - 'PUBLIC' or 'VPC'
      protocol           - 'HTTP', 'MCP' or 'A2A'
      execution_role_arn - IAM role the runtime assumes (config.AGENTCORE_ROLE_ARN,
                           created by the CloudFormation stack); the CLI then
                           does not create a role of its own

    The build settings (CodeZip, entry point, code location, Python version)
    are always (re)set so the CLI packages `build/runtime/`.
    Returns the updated runtime entry.
    """
    spec = read_project_config()
    rt = runtime_spec(spec)
    rt.update({
        'build':          'CodeZip',
        'entrypoint':     RUNTIME_ENTRYPOINT,
        'codeLocation':   os.path.relpath(RUNTIME_CODE_DIR, PROJECT_ROOT).replace(os.sep, '/') + '/',
        'runtimeVersion': RUNTIME_PYTHON,
        # The project ships its own X-Ray tracing (agent_observability.py); with
        # OTel auto-instrumentation off the entry point is started directly.
        'instrumentation': {'enableOtel': False},
    })
    if network_mode:
        rt['networkMode'] = network_mode
    if protocol:
        rt['protocol'] = protocol
    if execution_role_arn:
        rt['executionRoleArn'] = execution_role_arn
    if env_vars:
        merged = {e['name']: e['value'] for e in rt.get('envVars', []) if 'name' in e}
        # Empty values are skipped (e.g. KB IDs before Task 5) - re-running the
        # deploy pipeline after the KBs exist adds them.
        merged.update({k: str(v) for k, v in env_vars.items() if v not in (None, '')})
        rt['envVars'] = [{'name': k, 'value': v} for k, v in merged.items()]
    write_project_config(spec)
    return rt


def stack_name() -> str:
    """CloudFormation stack the CLI deploys: AgentCore-<project>-<target>."""
    project = config.AGENTCORE_PROJECT_NAME
    try:
        project = read_project_config().get('name', project)
    except FileNotFoundError:
        pass
    return f"AgentCore-{project.replace('_', '-')}-default"


# ─────────────────────────────────────────────────────
# RUNTIME CODE STAGING  (what the CLI packages)
# ─────────────────────────────────────────────────────

def stage_runtime_code() -> str:
    """
    Assemble build/runtime/ - the directory `agentcore deploy` packages:
    the src/ modules, config.py, a pyproject.toml with the runtime
    dependencies and the serve-mode marker file. Returns the directory.
    """
    missing = [p for p in RUNTIME_PROJECT_FILES if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(f"Cannot stage runtime code, missing: {missing}")

    if os.path.isdir(RUNTIME_CODE_DIR):
        shutil.rmtree(RUNTIME_CODE_DIR)
    os.makedirs(RUNTIME_CODE_DIR)

    for path in RUNTIME_PROJECT_FILES:
        shutil.copy2(path, os.path.join(RUNTIME_CODE_DIR, os.path.basename(path)))

    deps = ',\n'.join(f'  "{req}"' for req in RUNTIME_REQUIREMENTS)
    with open(os.path.join(RUNTIME_CODE_DIR, 'pyproject.toml'), 'w', encoding='utf-8') as fh:
        fh.write(
            '[project]\n'
            'name = "novamart-agentcore-runtime"\n'
            'version = "0.1.0"\n'
            'description = "NovaMart multi-agent support system - AgentCore Runtime package"\n'
            'requires-python = ">=3.12"\n'
            f'dependencies = [\n{deps},\n]\n'
        )
    with open(os.path.join(RUNTIME_CODE_DIR, RUNTIME_MARKER), 'w', encoding='utf-8') as fh:
        fh.write('agentcore runtime package\n')

    print(f"  Runtime code staged in {os.path.relpath(RUNTIME_CODE_DIR, PROJECT_ROOT)}/ "
          f"({len(RUNTIME_PROJECT_FILES)} files + pyproject.toml, entry point {RUNTIME_ENTRYPOINT})")
    return RUNTIME_CODE_DIR


# ─────────────────────────────────────────────────────
# DEPLOY / STATE
# ─────────────────────────────────────────────────────

def deploy(verbose: bool = False) -> None:
    """
    Run `agentcore deploy -y` (non-interactive). The CLI packages
    build/runtime/, synthesizes the CDK stack and creates or updates the
    AgentCore Runtime; it bootstraps CDK on the first run.
    """
    if not os.path.isdir(RUNTIME_CODE_DIR):
        stage_runtime_code()
    print(f"  Running: agentcore deploy -y   (CLI {cli_version()}, region {config.AWS_REGION})", flush=True)
    args = ['deploy', '-y'] + (['-v'] if verbose else [])
    run(*args)


def read_deployed_state() -> dict:
    try:
        with open(STATE_PATH, encoding='utf-8') as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {'targets': {}}


def deployed_runtime_arn() -> str:
    """
    ARN of the deployed runtime, or '' if it is not deployed.
    Read from agentcore/.cli/deployed-state.json (written by `agentcore
    deploy`), falling back to a lookup by name in the AWS account.
    """
    for target in read_deployed_state().get('targets', {}).values():
        runtimes = (target.get('resources') or {}).get('runtimes') or {}
        rt = runtimes.get(config.AGENTCORE_AGENT_NAME)
        if rt and rt.get('runtimeArn'):
            return rt['runtimeArn']
    try:
        import boto3
        ctl = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)
        for rt in ctl.list_agent_runtimes().get('agentRuntimes', []):
            if rt['agentRuntimeName'] == config.AGENTCORE_RUNTIME_NAME:
                return rt['agentRuntimeArn']
    except Exception:
        pass
    return ''


def reset_deployed_state() -> None:
    """Forget the deployed resources locally (used by infrastructure/cleanup.py)."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w', encoding='utf-8') as fh:
        json.dump({'targets': {}}, fh, indent=2)
        fh.write('\n')


if __name__ == '__main__':
    print(f"agentcore CLI: {cli_path()} (version {cli_version()})")
    print(f"project config: {CONFIG_PATH}")
    print(f"runtime name:   {config.AGENTCORE_RUNTIME_NAME}")
    print(f"stack name:     {stack_name()}")
    print(f"deployed ARN:   {deployed_runtime_arn() or '(not deployed)'}")
