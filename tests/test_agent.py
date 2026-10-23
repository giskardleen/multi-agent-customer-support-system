"""
test_agent.py
=============
Test suite for the Udacity AgentCore Project.

Run after each task to validate your implementation:
  python tests/test_agent.py task2    # Test multi-agent orchestration
  python tests/test_agent.py task3    # Test AgentCore deployment + guardrails
  python tests/test_agent.py task4    # Test memory
  python tests/test_agent.py task5    # Test Bedrock Knowledge Base configuration + retrieval
  python tests/test_agent.py task6    # Test observability
  python tests/test_agent.py all      # Run all tests

Every check in Tasks 3-6 reads real state back from AWS (guardrail policy
values, runtime status and environment, memory resource, Knowledge Base
storage, X-Ray / CloudWatch configuration). Nothing is mocked or assumed.
"""

import sys
import os
import re
import ast
import inspect
import time
import boto3
import unittest
from datetime import datetime, timedelta, timezone

# Add parent dir to path so we can import student files
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import config

# Import the student's module once, at module scope, so every task suite
# (not only Task 2) sees the same module and the same import errors.
try:
    import agent_orchestrator as ao
    _IMPORT_ERROR = None
except Exception as _exc:              # noqa: BLE001 - report any failure
    ao = None
    _IMPORT_ERROR = _exc

# ─────────────────────────────────────────────────────
# HELPER UTILITIES
# ─────────────────────────────────────────────────────

class Colors:
    GREEN  = '\033[92m'
    RED    = '\033[91m'
    YELLOW = '\033[93m'
    CYAN   = '\033[96m'
    BOLD   = '\033[1m'
    RESET  = '\033[0m'

def passed(msg):
    print(f"  {Colors.GREEN}✓ PASS{Colors.RESET} {msg}")

def failed(msg, detail=""):
    print(f"  {Colors.RED}✗ FAIL{Colors.RESET} {msg}")
    if detail:
        print(f"         {Colors.YELLOW}{detail}{Colors.RESET}")

def info(msg):
    print(f"  {Colors.CYAN}ℹ INFO{Colors.RESET} {msg}")

def header(title):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'─'*55}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'─'*55}{Colors.RESET}")

score = {'earned': 0, 'possible': 0}

def check(condition, points, pass_msg, fail_msg, detail=""):
    score['possible'] += points
    if condition:
        score['earned'] += points
        passed(f"[+{points}pts] {pass_msg}")
        return True
    else:
        failed(f"[+{points}pts] {fail_msg}", detail)
        return False


def _runtime_id() -> str:
    return config.AGENTCORE_RUNTIME_ARN.split('/')[-1]


def _get_runtime(agentcore_control):
    """Return the runtime description or None (prints the reason)."""
    runtime_arn = config.AGENTCORE_RUNTIME_ARN
    if not runtime_arn:
        failed("AGENTCORE_RUNTIME_ARN not set - complete Task 3 first",
               "Run `python src/agent_orchestrator.py deploy` and copy the ARN into .env")
        return None
    if not re.match(r'^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[A-Za-z0-9_-]+$', runtime_arn):
        failed("AGENTCORE_RUNTIME_ARN does not look like an AgentCore Runtime ARN", runtime_arn)
        return None
    try:
        return agentcore_control.get_agent_runtime(agentRuntimeId=_runtime_id())
    except Exception as e:
        failed("get_agent_runtime() failed for AGENTCORE_RUNTIME_ARN", str(e))
        return None


# ═══════════════════════════════════════════════════════
#  TASK 2 TESTS - Multi-Agent Orchestration
# ═══════════════════════════════════════════════════════

class TestTask2(unittest.TestCase):

    def setUp(self):
        """Student's agent_orchestrator module (imported at module scope)."""
        if ao is None:
            self.fail(f"Could not import agent_orchestrator: {_IMPORT_ERROR}")
        self.ao = ao

    def _get_model_id(self, agent):
        """Extract the model ID string from a Strands Agent's BedrockModel.

        Strands BedrockModel exposes config as a plain dict via model.config,
        with 'model_id' as a key. Falls back to a direct attribute check for
        future SDK versions.
        """
        model = getattr(agent, 'model', None)
        if model is None:
            return ''
        # Strands stores config as a dict: model.config['model_id']
        cfg = getattr(model, 'config', None)
        if isinstance(cfg, dict):
            val = cfg.get('model_id', '')
            if val:
                return val
        # Fallback: direct attribute (future SDK versions)
        val = getattr(model, 'model_id', '')
        if isinstance(val, str) and val:
            return val
        return ''

    def _get_tool_count(self, agent):
        """Count tools registered on a Strands Agent.

        Strands stores tools in agent.tool_registry (a ToolRegistry object)
        whose inner .registry attribute is a plain dict of {name: tool}.
        """
        tool_registry = getattr(agent, 'tool_registry', None)
        if tool_registry is not None:
            inner = getattr(tool_registry, 'registry', None)
            if isinstance(inner, dict):
                return len(inner)
        return 0

    def test_2_1_inventory_agent_instantiates(self):
        """InventoryAgent should return a Strands Agent object."""
        header("Task 2 - Multi-Agent Orchestration")
        try:
            agent = self.ao.build_inventory_agent()
            check(
                agent is not None,
                5,
                "build_inventory_agent() returns an Agent object",
                "build_inventory_agent() returned None",
                "Ensure you return Agent(...) at the end of the function"
            )
        except Exception as e:
            check(False, 5, "", "build_inventory_agent() raised an exception", str(e))

    def test_2_2_inventory_agent_has_tools(self):
        """InventoryAgent should have exactly 3 tools registered."""
        try:
            agent = self.ao.build_inventory_agent()
            tool_count = self._get_tool_count(agent)
            check(
                tool_count == 3,
                5,
                f"InventoryAgent has 3 tools ({tool_count} found)",
                f"InventoryAgent should have 3 tools, found {tool_count}",
                "Expected: check_order_status, get_customer_tier, list_customer_orders"
            )
        except Exception as e:
            check(False, 5, "", "Error checking InventoryAgent tools", str(e))

    def test_2_3_policy_agent_instantiates(self):
        """PolicyAgent should return a Strands Agent object."""
        try:
            agent = self.ao.build_policy_agent()
            check(
                agent is not None,
                5,
                "build_policy_agent() returns an Agent object",
                "build_policy_agent() returned None"
            )
        except Exception as e:
            check(False, 5, "", "build_policy_agent() raised an exception", str(e))

    def test_2_4_policy_agent_has_tool(self):
        """PolicyAgent should have 1 tool: search_all_policies."""
        try:
            agent = self.ao.build_policy_agent()
            tool_count = self._get_tool_count(agent)
            check(
                tool_count == 1,
                5,
                f"PolicyAgent has 1 tool ({tool_count} found)",
                f"PolicyAgent should have 1 tool, found {tool_count}",
                "Expected: search_all_policies"
            )
        except Exception as e:
            check(False, 5, "", "Error checking PolicyAgent tools", str(e))

    def test_2_5_orchestrator_instantiates(self):
        """OrchestratorAgent should return a Strands Agent object."""
        try:
            inventory  = self.ao.build_inventory_agent()
            refund     = self.ao.build_refund_agent()
            policy     = self.ao.build_policy_agent()
            comm       = self.ao.build_communication_agent()
            orchestrator = self.ao.build_orchestrator_agent(inventory, refund, policy, comm)
            check(
                orchestrator is not None,
                5,
                "build_orchestrator_agent() returns an Agent object",
                "build_orchestrator_agent() returned None"
            )
        except Exception as e:
            check(False, 5, "", "build_orchestrator_agent() raised an exception", str(e))

    def test_2_6_orchestrator_has_routing_tools(self):
        """OrchestratorAgent should have 5 routing tools."""
        try:
            inventory  = self.ao.build_inventory_agent()
            refund     = self.ao.build_refund_agent()
            policy     = self.ao.build_policy_agent()
            comm       = self.ao.build_communication_agent()
            orchestrator = self.ao.build_orchestrator_agent(inventory, refund, policy, comm)
            tool_count = self._get_tool_count(orchestrator)
            check(
                tool_count == 5,
                5,
                f"OrchestratorAgent has 5 routing tools ({tool_count} found)",
                f"OrchestratorAgent should have 5 tools, found {tool_count}",
                "Expected: initialize_session, route_to_inventory_agent, route_to_policy_agent, "
                "route_to_refund_agent, route_to_communication_agent"
            )
        except Exception as e:
            check(False, 5, "", "Error checking OrchestratorAgent tools", str(e))

    def test_2_7_routing_uses_different_models(self):
        """Orchestrator should use Haiku; Workers should use Sonnet."""
        try:
            inventory  = self.ao.build_inventory_agent()
            refund     = self.ao.build_refund_agent()
            policy     = self.ao.build_policy_agent()
            comm       = self.ao.build_communication_agent()
            orchestrator = self.ao.build_orchestrator_agent(inventory, refund, policy, comm)

            orchestrator_model = self._get_model_id(orchestrator)
            inventory_model    = self._get_model_id(inventory)

            uses_haiku  = orchestrator_model == config.ORCHESTRATOR_MODEL_ID
            uses_sonnet = inventory_model == config.WORKER_MODEL_ID

            check(
                uses_haiku,
                5,
                "OrchestratorAgent uses config.ORCHESTRATOR_MODEL_ID (Claude Haiku 4.5 - correct for routing)",
                "OrchestratorAgent should use config.ORCHESTRATOR_MODEL_ID (Claude Haiku 4.5)",
                f"Found model: {orchestrator_model}"
            )
            check(
                uses_sonnet,
                5,
                "Worker agents use config.WORKER_MODEL_ID (Claude Sonnet 4.5 - correct for reasoning)",
                "Worker agents should use config.WORKER_MODEL_ID (Claude Sonnet 4.5)",
                f"Found model: {inventory_model}"
            )
        except Exception as e:
            check(False, 10, "", "Error checking model assignments", str(e))

    def test_2_8_parallel_retrieval_uses_threadpool(self):
        """search_all_policies must fan out with ThreadPoolExecutor (static check)."""
        try:
            source = inspect.getsource(self.ao.build_policy_agent)
            tree   = ast.parse(source)
            uses_pool = any(
                isinstance(node, ast.Name) and node.id == 'ThreadPoolExecutor'
                or isinstance(node, ast.Attribute) and node.attr == 'ThreadPoolExecutor'
                for node in ast.walk(tree)
            )
            submits = sum(1 for node in ast.walk(tree)
                          if isinstance(node, ast.Attribute) and node.attr in ('submit', 'map'))
            check(
                uses_pool and submits >= 1,
                0,
                "build_policy_agent() runs the retrievers with ThreadPoolExecutor",
                "build_policy_agent() does not use ThreadPoolExecutor for the retrievers",
                "Rubric requires ThreadPoolExecutor(max_workers=3) + as_completed(); "
                "sequential retrieval fails the parallel-retrieval criterion"
            )
        except Exception as e:
            check(False, 0, "", "Could not inspect build_policy_agent()", str(e))


# ═══════════════════════════════════════════════════════
#  TASK 3 TESTS - AgentCore Deployment + Guardrails
# ═══════════════════════════════════════════════════════

class TestTask3(unittest.TestCase):

    def setUp(self):
        self.bedrock = boto3.client('bedrock', region_name=config.AWS_REGION)
        self.agentcore_control = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)

    def _get_guardrail(self):
        guardrail_id = config.GUARDRAIL_ID
        if not guardrail_id:
            failed("GUARDRAIL_ID not set in environment",
                   "Add GUARDRAIL_ID to your .env file (printed by the deploy command)")
            return None
        version = config.GUARDRAIL_VERSION or 'DRAFT'
        return self.bedrock.get_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion=version)

    def test_3_1_guardrail_exists(self):
        """A Bedrock Guardrail should exist with the correct name."""
        header("Task 3 - AgentCore Deployment + Guardrails")
        try:
            response = self.bedrock.list_guardrails()
            guardrails = response.get('guardrails', [])
            names = [g['name'] for g in guardrails]

            check(
                config.GUARDRAIL_NAME in names,
                5,
                f"Guardrail '{config.GUARDRAIL_NAME}' exists in Bedrock",
                f"Guardrail '{config.GUARDRAIL_NAME}' not found",
                f"Found guardrails: {names}"
            )
        except Exception as e:
            check(False, 5, "", "Error checking guardrail", str(e))

    def test_3_2_guardrail_has_required_policies(self):
        """Guardrail policies must have the values the project specifies."""
        try:
            g = self._get_guardrail()
            if g is None:
                check(False, 5, "", "Guardrail policies could not be read")
                return

            problems = []

            # Content filters
            expected_strength = {'SEXUAL': 'HIGH', 'VIOLENCE': 'HIGH', 'HATE': 'HIGH',
                                 'INSULTS': 'MEDIUM', 'MISCONDUCT': 'MEDIUM'}
            filters = {f['type']: f for f in g.get('contentPolicy', {}).get('filters', [])}
            for ftype, strength in expected_strength.items():
                f = filters.get(ftype)
                if not f:
                    problems.append(f"content filter {ftype} missing")
                elif f.get('inputStrength') != strength or f.get('outputStrength') != strength:
                    problems.append(f"content filter {ftype} should be {strength} "
                                    f"(found in={f.get('inputStrength')}, out={f.get('outputStrength')})")

            # PII entities
            expected_pii = {'CREDIT_DEBIT_CARD_NUMBER': 'BLOCK', 'US_SOCIAL_SECURITY_NUMBER': 'BLOCK',
                            'EMAIL': 'ANONYMIZE', 'PHONE': 'ANONYMIZE'}
            pii = {p['type']: p.get('action') for p in
                   g.get('sensitiveInformationPolicy', {}).get('piiEntities', [])}
            for ptype, action in expected_pii.items():
                if pii.get(ptype) != action:
                    problems.append(f"PII entity {ptype} should be {action} (found {pii.get(ptype)})")

            # Denied topics
            topics = g.get('topicPolicy', {}).get('topics', [])
            denied = [t for t in topics if t.get('type') == 'DENY']
            topic_text = ' '.join((t.get('name', '') + ' ' + t.get('definition', '')).lower() for t in denied)
            for keyword in ('competitor', 'pricing', 'legal'):
                if keyword not in topic_text:
                    problems.append(f"no DENY topic covering '{keyword}'")
            if len(denied) < 3:
                problems.append(f"expected 3 DENY topics, found {len(denied)}")

            # Managed profanity list
            managed = [w.get('type') for w in g.get('wordPolicy', {}).get('managedWordLists', [])]
            if 'PROFANITY' not in managed:
                problems.append("wordPolicy managed PROFANITY list not enabled")

            check(
                not problems,
                5,
                "Guardrail content, PII, topic and word policies match the specification",
                "Guardrail policies do not match the specification",
                '; '.join(problems)
            )
        except Exception as e:
            check(False, 5, "", "Error validating guardrail policies", str(e))

    def test_3_3_guardrail_is_versioned(self):
        """GUARDRAIL_VERSION must be a published (non-DRAFT) version."""
        try:
            version = config.GUARDRAIL_VERSION
            versioned = bool(version) and version != 'DRAFT' and version.isdigit()
            if versioned:
                g = self.bedrock.get_guardrail(guardrailIdentifier=config.GUARDRAIL_ID,
                                               guardrailVersion=version)
                versioned = g.get('status') == 'READY'
            check(
                versioned,
                3,
                f"Guardrail version {version} is published and READY",
                f"GUARDRAIL_VERSION is '{version}' - expected a numbered version",
                "Call create_guardrail_version() in create_guardrail() and copy the returned version into .env"
            )
        except Exception as e:
            check(False, 3, "", "Error checking guardrail version", str(e))

    def test_3_4_agentcore_runtime_ready(self):
        """The AgentCore Runtime must exist, be READY and carry the required configuration."""
        try:
            runtime = _get_runtime(self.agentcore_control)
            if runtime is None:
                check(False, 7, "", "AgentCore Runtime is not deployed")
                return

            status   = runtime.get('status')
            network  = runtime.get('networkConfiguration', {}).get('networkMode')
            protocol = runtime.get('protocolConfiguration', {}).get('serverProtocol')
            env      = runtime.get('environmentVariables', {}) or {}

            problems = []
            if status != 'READY':
                problems.append(f"status is {status}")
            if network != 'PUBLIC':
                problems.append(f"networkMode is {network}, expected PUBLIC")
            if protocol != 'HTTP':
                problems.append(f"serverProtocol is {protocol}, expected HTTP")
            for key in ('AWS_REGION', 'PROJECT_NAME', 'RETURNS_KB_ID', 'SHIPPING_KB_ID',
                        'WARRANTY_KB_ID', 'AGENT_LOG_GROUP', 'GUARDRAIL_ID', 'GUARDRAIL_VERSION'):
                if not env.get(key):
                    problems.append(f"environment variable {key} missing")
            if config.GUARDRAIL_ID and env.get('GUARDRAIL_ID') and env['GUARDRAIL_ID'] != config.GUARDRAIL_ID:
                problems.append("GUARDRAIL_ID on the runtime differs from .env")

            check(
                not problems,
                7,
                f"AgentCore Runtime is READY (PUBLIC / HTTP) with guardrail + KB environment variables",
                "AgentCore Runtime configuration is incomplete",
                '; '.join(problems)
            )
        except Exception as e:
            check(False, 7, "", "Error checking AgentCore Runtime", str(e))


# ═══════════════════════════════════════════════════════
#  TASK 4 TESTS - Memory
# ═══════════════════════════════════════════════════════

class TestTask4(unittest.TestCase):

    def setUp(self):
        self.agentcore_control = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)

    def test_4_1_memory_is_configured(self):
        """An AgentCore Memory resource with a SESSION_SUMMARY strategy must exist."""
        header("Task 4 - Memory")
        try:
            memories = self.agentcore_control.list_memories().get('memories', [])
            match = [m for m in memories if m['id'].startswith(config.MEMORY_NAME)]
            if not match:
                check(False, 15, "", f"No AgentCore Memory named '{config.MEMORY_NAME}*' found",
                      f"configure_memory() must call create_memory(name=config.MEMORY_NAME, ...). "
                      f"Found: {[m['id'] for m in memories]}")
                return

            memory = self.agentcore_control.get_memory(memoryId=match[0]['id'])['memory']
            strategies = memory.get('strategies', [])
            types = [s.get('type') for s in strategies]

            check(
                memory.get('status') == 'ACTIVE',
                5,
                f"AgentCore Memory {memory['id']} is ACTIVE",
                f"AgentCore Memory status is {memory.get('status')}",
                memory.get('failureReason', '')
            )
            check(
                'SUMMARIZATION' in types,
                5,
                "Memory uses the SESSION_SUMMARY (summaryMemoryStrategy) strategy",
                "Memory does not have a summaryMemoryStrategy",
                f"Found strategy types: {types}"
            )
            check(
                memory.get('eventExpiryDuration') == 7,
                5,
                "Memory eventExpiryDuration is 7 days",
                f"Memory eventExpiryDuration is {memory.get('eventExpiryDuration')}, expected 7",
            )
        except Exception as e:
            check(False, 15, "", "Error checking memory configuration", str(e))


# ═══════════════════════════════════════════════════════
#  TASK 5 TESTS - Bedrock Knowledge Bases
# ═══════════════════════════════════════════════════════

class TestTask5(unittest.TestCase):

    def setUp(self):
        self.bedrock_agent = boto3.client('bedrock-agent', region_name=config.AWS_REGION)

    def _check_kb(self, label: str, env_key: str, kb_id: str, points: int):
        check(
            bool(kb_id),
            points,
            f"{env_key} is set in environment ({kb_id})",
            f"{env_key} is not set - create the {label} Knowledge Base in AWS Console and add the ID to .env"
        )
        if not kb_id:
            return
        try:
            kb = self.bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id).get('knowledgeBase', {})
            status = kb.get('status', 'UNKNOWN')
            check(
                status == 'ACTIVE',
                0,
                f"{label} Knowledge Base is ACTIVE",
                f"{label} Knowledge Base status is {status} - sync the data source in AWS Console"
            )

            emb = kb.get('knowledgeBaseConfiguration', {}).get(
                'vectorKnowledgeBaseConfiguration', {}).get('embeddingModelArn', '')
            check(
                'titan-embed-text-v2' in emb,
                0,
                f"{label} KB uses amazon.titan-embed-text-v2:0",
                f"{label} KB embedding model is {emb or 'unknown'} - expected amazon.titan-embed-text-v2:0"
            )

            synced = False
            for ds in self.bedrock_agent.list_data_sources(knowledgeBaseId=kb_id).get('dataSourceSummaries', []):
                jobs = self.bedrock_agent.list_ingestion_jobs(
                    knowledgeBaseId=kb_id, dataSourceId=ds['dataSourceId']).get('ingestionJobSummaries', [])
                if any(j.get('status') == 'COMPLETE' for j in jobs):
                    synced = True
            check(
                synced,
                0,
                f"{label} KB data source has a completed sync",
                f"{label} KB has no completed ingestion job - click Sync in the AWS Console"
            )
        except Exception as e:
            check(False, 0, "", f"Error verifying {label} KB: {e}")

    def test_5_1_returns_kb_configured(self):
        header("Task 5 - Bedrock Knowledge Bases")
        self._check_kb('Returns', 'RETURNS_KB_ID', config.RETURNS_KB_ID, 5)

    def test_5_2_shipping_kb_configured(self):
        self._check_kb('Shipping', 'SHIPPING_KB_ID', config.SHIPPING_KB_ID, 5)

    def test_5_3_warranty_kb_configured(self):
        self._check_kb('Warranty', 'WARRANTY_KB_ID', config.WARRANTY_KB_ID, 5)

    def test_5_4_parallel_retrieval(self):
        """search_all_policies() must return non-empty results from all three KBs."""
        query = "What is the return window for premium customers and how long is the warranty?"
        try:
            from bedrock_kb_retrieval import retrieve_from_knowledge_base
            missing = []
            for label, kb_id in (('Returns', config.RETURNS_KB_ID),
                                 ('Shipping', config.SHIPPING_KB_ID),
                                 ('Warranty', config.WARRANTY_KB_ID)):
                results = retrieve_from_knowledge_base(kb_id, query) if kb_id else []
                ok = bool(results) and results[0].get('source') != 'error'
                if not ok:
                    missing.append(f"{label} ({results[0]['text'] if results else 'no KB id / empty'})")
            check(
                not missing,
                5,
                "All three Knowledge Bases return passages for a policy question",
                "Some Knowledge Bases returned no passages",
                '; '.join(missing)
            )
        except Exception as e:
            check(False, 5, "", "Error retrieving from Knowledge Bases", str(e))
            return

        try:
            if ao is None:
                raise RuntimeError(f"agent_orchestrator import failed: {_IMPORT_ERROR}")
            policy_agent = ao.build_policy_agent()
            t0 = time.time()
            result = policy_agent.tool.search_all_policies(query=query, record_direct_tool_call=False)
            elapsed = time.time() - t0
            text = ' '.join(c.get('text', '') for c in result.get('content', []) if isinstance(c, dict))
            ok = result.get('status') == 'success' and len(text.strip()) > 100 \
                and 'retrieval failed' not in text.lower()
            check(
                ok,
                5,
                f"search_all_policies() returned combined results from the three retrievers ({elapsed:.1f}s)",
                "search_all_policies() did not return combined results",
                f"status={result.get('status')} text={text[:200]!r}"
            )
        except Exception as e:
            check(False, 5, "", "Error calling search_all_policies()", str(e))


# ═══════════════════════════════════════════════════════
#  TASK 6 TESTS - Observability
# ═══════════════════════════════════════════════════════

class TestTask6(unittest.TestCase):

    def setUp(self):
        self.agentcore_control = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)
        self.logs = boto3.client('logs', region_name=config.AWS_REGION)
        self.xray = boto3.client('xray', region_name=config.AWS_REGION)

    def test_6_1_cloudwatch_logging_enabled(self):
        """CloudWatch logging must be configured on the runtime and the log group must exist."""
        header("Task 6 - Observability")
        try:
            runtime = _get_runtime(self.agentcore_control)
            if runtime is None:
                check(False, 10, "", "AgentCore Runtime not available - complete Task 3 first")
                return
            env = runtime.get('environmentVariables', {}) or {}
            log_group = env.get('AGENT_LOG_GROUP', '')
            problems = []
            if log_group != config.AGENT_LOG_GROUP:
                problems.append(f"AGENT_LOG_GROUP is {log_group!r}, expected {config.AGENT_LOG_GROUP!r}")
            if env.get('AGENT_LOG_LEVEL', '').upper() != 'INFO':
                problems.append(f"AGENT_LOG_LEVEL is {env.get('AGENT_LOG_LEVEL')!r}, expected 'INFO'")
            if env.get('AGENT_LOG_TO_CLOUDWATCH', '').lower() != 'true':
                problems.append("cloudWatchConfig.enabled was not True")
            groups = self.logs.describe_log_groups(logGroupNamePrefix=config.AGENT_LOG_GROUP).get('logGroups', [])
            if not any(g['logGroupName'] == config.AGENT_LOG_GROUP for g in groups):
                problems.append(f"log group {config.AGENT_LOG_GROUP} does not exist")
            check(
                not problems,
                10,
                f"CloudWatch logging enabled at INFO level → {config.AGENT_LOG_GROUP}",
                "CloudWatch logging is not configured on the runtime",
                '; '.join(problems) + "  (run configure_observability() via the deploy command)"
            )
            streams = self.logs.describe_log_streams(logGroupName=config.AGENT_LOG_GROUP,
                                                     orderBy='LastEventTime', descending=True,
                                                     limit=1).get('logStreams', [])
            if streams and streams[0].get('lastEventTimestamp'):
                age = (time.time() * 1000 - streams[0]['lastEventTimestamp']) / 60000
                info(f"latest agent log stream: {streams[0]['logStreamName']} ({age:.0f} min ago)")
            else:
                info("no agent log events yet - run `python src/agent_orchestrator.py test`")
        except Exception as e:
            check(False, 10, "", "Error checking CloudWatch config", str(e))

    def test_6_2_xray_tracing_enabled(self):
        """X-Ray tracing must be enabled at 100% sampling (runtime + Transaction Search)."""
        try:
            runtime = _get_runtime(self.agentcore_control)
            if runtime is None:
                check(False, 10, "", "AgentCore Runtime not available")
                return
            env = runtime.get('environmentVariables', {}) or {}
            problems = []
            if env.get('AGENT_TRACING_ENABLED', '').lower() != 'true':
                problems.append("xRayConfig.enabled was not True on the runtime")
            try:
                rate = float(env.get('AGENT_TRACE_SAMPLING_RATE', '0'))
            except ValueError:
                rate = 0.0
            if rate != 1.0:
                problems.append(f"xRayConfig.samplingRate is {rate}, expected 1.0")

            dest = self.xray.get_trace_segment_destination()
            if dest.get('Destination') != 'CloudWatchLogs':
                problems.append(f"Transaction Search destination is {dest.get('Destination')} "
                                f"(expected CloudWatchLogs)")
            rules = self.xray.get_indexing_rules().get('IndexingRules', [])
            pct = next((r.get('Rule', {}).get('Probabilistic', {}).get('DesiredSamplingPercentage')
                        for r in rules if r.get('Name') == 'Default'), None)
            if pct != 100:
                problems.append(f"trace indexing percentage is {pct}, expected 100")

            check(
                not problems,
                10,
                "X-Ray tracing enabled: 100% sampling, Transaction Search → CloudWatch Logs "
                f"({dest.get('Status')})",
                "X-Ray tracing is not fully configured",
                '; '.join(problems)
            )
        except Exception as e:
            check(False, 10, "", "Error checking X-Ray config", str(e))

    def test_6_3_traces_present(self):
        """Informational: were traces from the multi-agent system received by X-Ray recently?"""
        try:
            now = datetime.now(timezone.utc)
            resp = self.xray.get_trace_summaries(
                StartTime=now - timedelta(hours=6), EndTime=now,
                FilterExpression='service("NovaMart-Orchestrator")',
            )
            count = len(resp.get('TraceSummaries', []))
            if count:
                info(f"{count} NovaMart trace(s) received by X-Ray in the last 6 hours - "
                     f"open CloudWatch → X-Ray traces → Service map for the screenshot")
            else:
                info("no NovaMart traces in X-Ray yet - run `python src/agent_orchestrator.py test` "
                     "(or `invoke`) and wait ~60 s")
        except Exception as e:
            info(f"could not query X-Ray traces: {e}")


# ═══════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════

TASK_SUITES = {
    'task2': TestTask2,
    'task3': TestTask3,
    'task4': TestTask4,
    'task5': TestTask5,
    'task6': TestTask6,
}

def run_task(task_name: str):
    suite = unittest.TestLoader().loadTestsFromTestCase(TASK_SUITES[task_name])
    unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, 'w', encoding='utf-8')).run(suite)

def print_score():
    print(f"\n{'═'*55}")
    pct = (score['earned'] / score['possible'] * 100) if score['possible'] > 0 else 0
    color = Colors.GREEN if pct >= 70 else Colors.YELLOW if pct >= 50 else Colors.RED
    print(f"  {Colors.BOLD}Score: {color}{score['earned']}/{score['possible']} pts ({pct:.0f}%){Colors.RESET}")
    print(f"{'═'*55}\n")


if __name__ == '__main__':
    arg = sys.argv[1] if len(sys.argv) > 1 else 'all'

    if arg == 'all':
        tasks = ['task2', 'task3', 'task4', 'task5', 'task6']
    elif arg in TASK_SUITES:
        tasks = [arg]
    else:
        print(f"Unknown argument: {arg}")
        print(f"Usage: python test_agent.py [{'|'.join(TASK_SUITES.keys())}|all]")
        sys.exit(1)

    if _IMPORT_ERROR is not None:
        print(f"{Colors.RED}Could not import src/agent_orchestrator.py: {_IMPORT_ERROR}{Colors.RESET}")

    for task in tasks:
        run_task(task)

    print_score()

    if score['earned'] == score['possible']:
        print(f"  {Colors.GREEN}{Colors.BOLD}🎉 Perfect score! All tasks complete.{Colors.RESET}")
    elif score['earned'] >= score['possible'] * 0.7:
        print(f"  {Colors.YELLOW}{Colors.BOLD}Good progress! Review failed checks above.{Colors.RESET}")
    else:
        print(f"  {Colors.RED}Keep going - re-read the TODO comments carefully.{Colors.RESET}")
    print()
