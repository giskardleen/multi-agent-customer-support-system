"""
config.py
=========
Central configuration for the Udacity AgentCore project.
Reads resource names and ARNs from CloudFormation stack exports to avoid
hard-coded AWS resource identifiers.

Bedrock Knowledge Base IDs are supplied through environment variables because
the Knowledge Bases are created separately in the AWS Console.

Values are resolved LAZILY: importing this module makes no AWS calls.
The first access to a CloudFormation-backed constant (e.g. config.ORDERS_TABLE)
loads the stack exports once and caches them. That keeps unit tests, linters
and `python -c "import config"` working without credentials, and gives a
distinct error message for each failure mode (no credentials / stack not
deployed / export missing).
"""

import os
from dotenv import load_dotenv

# Load environment-specific resource IDs from .env when present.
load_dotenv()

# ─────────────────────────────────────────────
# REGION & PROJECT SETTINGS
# ─────────────────────────────────────────────
AWS_REGION   = os.environ.get('AWS_REGION', 'us-east-1')
PROJECT_NAME = os.environ.get('PROJECT_NAME', 'udacity-agentcore')

# ─────────────────────────────────────────────
# FOUNDATION MODELS
# ─────────────────────────────────────────────
# Orchestrator agent: Claude Haiku 4.5 - fast, cost-efficient routing decisions
ORCHESTRATOR_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Worker agents: Claude Sonnet 4.5 - more capable for reasoning and generation
WORKER_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# ─────────────────────────────────────────────
# CLOUDFORMATION EXPORTS LOADER (lazy, cached)
# ─────────────────────────────────────────────
_exports_cache = None


class ConfigError(RuntimeError):
    """Raised when a required AWS resource identifier cannot be resolved."""


def _load_cf_exports() -> dict:
    """Load all CloudFormation stack exports into a dict (cached after first call)."""
    global _exports_cache
    if _exports_cache is not None:
        return _exports_cache

    import boto3
    from botocore.exceptions import NoCredentialsError, ClientError, EndpointConnectionError

    try:
        cf = boto3.client('cloudformation', region_name=AWS_REGION)
        exports = {}
        for page in cf.get_paginator('list_exports').paginate():
            for export in page['Exports']:
                exports[export['Name']] = export['Value']
    except NoCredentialsError as exc:
        raise ConfigError(
            "No AWS credentials found. Configure the AWS CLI (aws configure) or "
            "set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY before running the project."
        ) from exc
    except EndpointConnectionError as exc:
        raise ConfigError(f"Cannot reach CloudFormation in region '{AWS_REGION}': {exc}") from exc
    except ClientError as exc:
        raise ConfigError(f"CloudFormation ListExports failed: {exc}") from exc

    if not any(name.startswith(f"{PROJECT_NAME}-") for name in exports):
        raise ConfigError(
            f"No CloudFormation exports found for stack '{PROJECT_NAME}' in {AWS_REGION}. "
            f"Deploy infrastructure/starter_stack.yaml (stack name '{PROJECT_NAME}') first."
        )
    _exports_cache = exports
    return exports


def _get(key: str, fallback_env: str = None) -> str:
    """Get a CloudFormation export value, with optional env var fallback."""
    value = _load_cf_exports().get(f"{PROJECT_NAME}-{key}")
    if not value and fallback_env:
        value = os.environ.get(fallback_env)
    if not value:
        raise ConfigError(
            f"Could not find CloudFormation export '{PROJECT_NAME}-{key}'. "
            f"Ensure the infrastructure stack is deployed (and up to date)."
        )
    return value


def _get_env(key: str, required: bool = True) -> str:
    """Get a value from environment variables (for resources not in CloudFormation)."""
    value = os.environ.get(key, '')
    if not value and required:
        raise ConfigError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill in your values."
        )
    return value


def _account_id() -> str:
    import boto3
    return boto3.client('sts', region_name=AWS_REGION).get_caller_identity()['Account']


def _get_kb_id(cf_key: str, env_key: str) -> str:
    """Try CloudFormation export first, then fall back to env var. Never raises."""
    value = os.environ.get(env_key, '')
    if value:
        return value
    try:
        return _load_cf_exports().get(f"{PROJECT_NAME}-{cf_key}", '')
    except ConfigError:
        return ''


def _get_optional_export(cf_key: str, env_key: str, default: str = '') -> str:
    """Env var first, then CloudFormation export, then default. Never raises."""
    value = os.environ.get(env_key, '')
    if value:
        return value
    try:
        return _load_cf_exports().get(f"{PROJECT_NAME}-{cf_key}", default)
    except ConfigError:
        return default


# ─────────────────────────────────────────────
# LAZY CONSTANTS
# Accessed as config.ORDERS_TABLE etc. - resolved on first use (PEP 562).
# ─────────────────────────────────────────────
_LAZY = {
    # AWS account
    'ACCOUNT_ID':           _account_id,

    # DynamoDB
    'ORDERS_TABLE':         lambda: _get('OrdersTable'),
    'CUSTOMERS_TABLE':      lambda: _get('CustomersTable'),
    'WORKFLOW_STATE_TABLE': lambda: _get('WorkflowStateTable'),

    # S3 (policy documents + deployment artifacts)
    'POLICY_BUCKET':        lambda: _get('PolicyBucket'),

    # S3 Vectors (Knowledge Base backing store - Task 5)
    'VECTOR_STORE_BUCKET':  lambda: _get('VectorBucket'),
    'VECTOR_STORE_BUCKET_ARN': lambda: _get('VectorBucketArn'),
    'RETURNS_VECTOR_INDEX':  lambda: _get('ReturnsVectorIndex'),
    'SHIPPING_VECTOR_INDEX': lambda: _get('ShippingVectorIndex'),
    'WARRANTY_VECTOR_INDEX': lambda: _get('WarrantyVectorIndex'),

    # IAM
    'AGENTCORE_ROLE_ARN':   lambda: _get('AgentCoreRoleArn'),

    # CloudWatch
    'AGENT_LOG_GROUP':      lambda: _get('AgentLogGroup'),

    # Bedrock Knowledge Base IDs (Task 5) - .env first, CloudFormation export second
    'RETURNS_KB_ID':        lambda: _get_kb_id('ReturnsKbId',  'RETURNS_KB_ID'),
    'SHIPPING_KB_ID':       lambda: _get_kb_id('ShippingKbId', 'SHIPPING_KB_ID'),
    'WARRANTY_KB_ID':       lambda: _get_kb_id('WarrantyKbId', 'WARRANTY_KB_ID'),

    # Task 3: Guardrail - .env first (printed by the deploy command), export second
    'GUARDRAIL_ID':         lambda: _get_optional_export('GuardrailId',      'GUARDRAIL_ID'),
    'GUARDRAIL_VERSION':    lambda: _get_optional_export('GuardrailVersion', 'GUARDRAIL_VERSION'),
}


def __getattr__(name: str):
    if name in _LAZY:
        value = _LAZY[name]()
        if name not in ('RETURNS_KB_ID', 'SHIPPING_KB_ID', 'WARRANTY_KB_ID',
                        'GUARDRAIL_ID', 'GUARDRAIL_VERSION'):
            globals()[name] = value          # cache stable resource names
        return value
    raise AttributeError(f"module 'config' has no attribute '{name}'")


# ─────────────────────────────────────────────
# DEPLOYED RESOURCE VALUES
# ─────────────────────────────────────────────

# Task 3: Filled in after deploying AgentCore Runtime
AGENTCORE_RUNTIME_ARN = os.environ.get('AGENTCORE_RUNTIME_ARN', '')

# Task 3: AgentCore CLI project (agentcore/agentcore.json) - the CLI names the
# deployed runtime "<project name>_<agent name>". Keep these in sync with the
# "name" fields in agentcore/agentcore.json.
AGENTCORE_PROJECT_NAME = 'udacity'             # agentcore.json  -> "name"
AGENTCORE_AGENT_NAME   = 'agentcore_runtime'   # agentcore.json  -> runtimes[0].name

# Task 3: AgentCore Runtime name as created by the CLI (underscores - AgentCore
# does not allow hyphens) -> udacity_agentcore_runtime
AGENTCORE_RUNTIME_NAME = f"{AGENTCORE_PROJECT_NAME}_{AGENTCORE_AGENT_NAME}"

# CloudFormation stack the CLI deploys the runtime with (AgentCore-<project>-<target>)
AGENTCORE_STACK_NAME = f"AgentCore-{AGENTCORE_PROJECT_NAME}-default"

# Task 4: AgentCore Memory name (underscores - AgentCore does not allow hyphens)
MEMORY_NAMESPACE = f"{PROJECT_NAME}-memory"
MEMORY_NAME      = MEMORY_NAMESPACE.replace('-', '_')

# ─────────────────────────────────────────────
# GUARDRAIL SETTINGS
# ─────────────────────────────────────────────
GUARDRAIL_NAME = f"{PROJECT_NAME}-guardrail"
GUARDRAIL_BLOCKED_TOPICS = [
    "competitor products",
    "pricing negotiations",
    "legal threats",
]

# ─────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────
def print_config():
    """Pretty-print the current configuration for debugging."""
    def _display(label: str, value: str, placeholder: str = "(not yet set)") -> None:
        print(f"  {label:<26} {value or placeholder}")

    def _resolve(name: str) -> str:
        try:
            return __getattr__(name)
        except Exception as exc:
            return f"ERROR: {exc}"

    print("\n" + "="*60)
    print("  Udacity AgentCore Project Configuration")
    print("="*60)
    _display("Region:",              AWS_REGION)
    _display("Account ID:",          _resolve('ACCOUNT_ID'))
    _display("Orchestrator Model:",  ORCHESTRATOR_MODEL_ID)
    _display("Worker Model:",        WORKER_MODEL_ID)
    print("  " + "-"*56)
    _display("Orders Table:",        _resolve('ORDERS_TABLE'))
    _display("Customers Table:",     _resolve('CUSTOMERS_TABLE'))
    _display("Workflow State Table:", _resolve('WORKFLOW_STATE_TABLE'))
    _display("Policy Bucket:",       _resolve('POLICY_BUCKET'))
    _display("Vector Bucket:",       _resolve('VECTOR_STORE_BUCKET'))
    _display("Vector Indexes:",      ", ".join(_resolve(n) for n in
                                     ('RETURNS_VECTOR_INDEX', 'SHIPPING_VECTOR_INDEX', 'WARRANTY_VECTOR_INDEX')))
    _display("AgentCore Role:",      _resolve('AGENTCORE_ROLE_ARN'))
    _display("Agent Log Group:",     _resolve('AGENT_LOG_GROUP'))
    print("  " + "-"*56)
    _display("Returns KB ID:",       _resolve('RETURNS_KB_ID'),  "(not yet created)")
    _display("Shipping KB ID:",      _resolve('SHIPPING_KB_ID'), "(not yet created)")
    _display("Warranty KB ID:",      _resolve('WARRANTY_KB_ID'), "(not yet created)")
    print("  " + "-"*56)
    _display("Runtime Name:",        AGENTCORE_RUNTIME_NAME)
    _display("Runtime ARN:",         AGENTCORE_RUNTIME_ARN, "(not yet deployed)")
    _display("Guardrail ID:",        _resolve('GUARDRAIL_ID'),      "(not yet created)")
    _display("Guardrail Version:",   _resolve('GUARDRAIL_VERSION'), "(not yet created)")
    print("="*60 + "\n")


if __name__ == '__main__':
    print_config()
