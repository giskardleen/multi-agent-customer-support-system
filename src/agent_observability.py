"""
agent_observability.py
======================
Observability layer for the multi-agent system.

It makes the multi-agent call chain visible in AWS:

  1. X-Ray tracing (AgentTracer + the `tool` decorator)
     Every request handled by the orchestrator becomes one X-Ray trace.
     The orchestrator's routing tools open *remote* subsegments named after
     the worker agent they call (InventoryAgent, PolicyAgent, RefundAgent,
     CommunicationAgent), and Knowledge Base retrievals open remote
     subsegments named after the KB (KnowledgeBase:returns, ...). X-Ray
     draws each remote subsegment as its own node, so the Service Map shows

         NovaMart-Orchestrator -> InventoryAgent
                               -> RefundAgent
                               -> PolicyAgent -> KnowledgeBase:returns
                                              -> KnowledgeBase:shipping
                                              -> KnowledgeBase:warranty
                               -> CommunicationAgent

     Segments are published with xray:PutTraceSegments from local commands and
     from inside the deployed AgentCore Runtime.

  2. CloudWatch Logs (setup_logging)
     INFO-level agent logs (tool calls, timings, trace ids) are shipped to
     the project log group (config.AGENT_LOG_GROUP) via logs:PutLogEvents.

  3. apply_observability_config()
     Applies the loggingConfiguration from configure_observability() to AWS:
       - X-Ray: enables CloudWatch Transaction Search (the mechanism AgentCore
         Observability uses), provisions its aws/spans log group and access
         policy, and sets the trace indexing percentage from
         xRayConfig.samplingRate
       - AgentCore Runtime: stores the log group / log level / tracing flags
         as runtime environment variables (declared in agentcore/agentcore.json
         and applied with `agentcore deploy` via agentcore_cli.py) so the
         deployed agent logs and traces exactly as configured
     tests/test_agent.py task6 reads that state back from AWS.

Nothing in this module fabricates a response: if an AWS call fails the
failure is printed and the program continues without tracing.
"""

import contextvars
import functools
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Optional

import boto3

from strands import tool as _strands_tool

# Ensure parent directory is on sys.path so config.py is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

logger = logging.getLogger('novamart.observability')


# ─────────────────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────────────────
# Read from environment variables so local and AgentCore Runtime execution use
# the same settings.

ENV_LOG_GROUP        = 'AGENT_LOG_GROUP'
ENV_LOG_LEVEL        = 'AGENT_LOG_LEVEL'
ENV_LOG_TO_CLOUDWATCH = 'AGENT_LOG_TO_CLOUDWATCH'
ENV_TRACING_ENABLED  = 'AGENT_TRACING_ENABLED'
ENV_SAMPLING_RATE    = 'AGENT_TRACE_SAMPLING_RATE'

SERVICE_NAME = 'NovaMart-Orchestrator'
TRANSACTION_SEARCH_SPAN_LOG_GROUP = 'aws/spans'
TRANSACTION_SEARCH_APPLICATION_LOG_GROUP = '/aws/application-signals/data'
TRANSACTION_SEARCH_POLICY_NAME = 'NovaMartTransactionSearchXRayAccess'

# Routing tool -> (X-Ray node name) : these become separate nodes on the map
_AGENT_NODE_FOR_TOOL = {
    'route_to_inventory_agent':     'InventoryAgent',
    'route_to_policy_agent':        'PolicyAgent',
    'route_to_refund_agent':        'RefundAgent',
    'route_to_communication_agent': 'CommunicationAgent',
}
# Tools whose subsegment may adopt children opened from other threads
# (see _resolve_parent). search_all_policies fans out to worker threads.
_FANOUT_TOOLS = {'search_all_policies'}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────
# X-RAY TRACER
# ─────────────────────────────────────────────────────

def _hex(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


def _new_trace_id() -> str:
    return f"1-{int(time.time()):08x}-{_hex(12)}"


class _Node:
    """One segment/subsegment document being built."""

    __slots__ = ('name', 'id', 'namespace', 'start', 'end', 'children',
                 'parent', 'thread_id', 'fallback_ok', 'error', 'fault',
                 'metadata', 'annotations')

    def __init__(self, name: str, namespace: Optional[str], parent, fallback_ok: bool):
        self.name        = name
        self.id          = _hex(8)
        self.namespace   = namespace
        self.start       = time.time()
        self.end         = None
        self.children    = []
        self.parent      = parent
        self.thread_id   = threading.get_ident()
        self.fallback_ok = fallback_ok
        self.error       = False
        self.fault       = False
        self.metadata    = {}
        self.annotations = {}

    def to_doc(self) -> dict:
        doc = {
            'name':       self.name,
            'id':         self.id,
            'start_time': self.start,
            'end_time':   self.end or time.time(),
        }
        if self.namespace:
            doc['namespace'] = self.namespace
        if self.error:
            doc['error'] = True
        if self.fault:
            doc['fault'] = True
        if self.annotations:
            doc['annotations'] = self.annotations
        if self.metadata:
            doc['metadata'] = {'novamart': self.metadata}
        if self.children:
            doc['subsegments'] = [c.to_doc() for c in self.children]
        return doc


class AgentTracer:
    """
    Builds one X-Ray segment per orchestrator request and publishes it with
    PutTraceSegments when the request finishes.

    Parent resolution for nested subsegments:
      1. the current node in this thread's context (contextvars), else
      2. the innermost open node created by this same thread, else
      3. the innermost open node that allows adoption (the root segment, an
         agent node opened by a route_to_* tool, or search_all_policies).
    Step 3 keeps the graph connected when Strands or the
    ThreadPoolExecutor runs tools on threads that did not inherit context.
    """

    def __init__(self):
        self._current: contextvars.ContextVar = contextvars.ContextVar('novamart_trace_node', default=None)
        self._lock  = threading.Lock()
        self._open: list = []          # open nodes, outermost first
        self._root: Optional[_Node] = None
        self._trace_id: Optional[str] = None
        self._client = None
        self.last_trace_id: Optional[str] = None
        self.last_published: bool = False

    # ── configuration ────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return _env_flag(ENV_TRACING_ENABLED, True)

    @property
    def sampling_rate(self) -> float:
        return max(0.0, min(1.0, _env_float(ENV_SAMPLING_RATE, 1.0)))

    def _xray(self):
        if self._client is None:
            self._client = boto3.client('xray', region_name=config.AWS_REGION)
        return self._client

    # ── parent resolution ────────────────────────────────────────────────
    def _resolve_parent(self) -> Optional[_Node]:
        node = self._current.get()
        if node is not None and node.end is None:
            return node
        tid = threading.get_ident()
        with self._lock:
            for n in reversed(self._open):
                if n.thread_id == tid:
                    return n
            for n in reversed(self._open):
                if n.fallback_ok:
                    return n
        return None

    def _push(self, node: _Node):
        with self._lock:
            self._open.append(node)
        return self._current.set(node)

    def _pop(self, node: _Node, token):
        node.end = time.time()
        with self._lock:
            if node in self._open:
                self._open.remove(node)
        try:
            self._current.reset(token)
        except (ValueError, LookupError):
            # token created in another context (thread) - nothing to reset
            pass

    # ── public API ───────────────────────────────────────────────────────
    @contextmanager
    def trace_request(self, session_id: str, customer_id: str, request: str = ''):
        """Open the root segment for one customer request."""
        if not self.enabled or self._root is not None:
            yield None
            return
        import random
        sampled = random.random() < self.sampling_rate
        root = _Node(SERVICE_NAME, None, None, fallback_ok=True)
        root.annotations = {'session_id': session_id, 'customer_id': customer_id,
                            'project': config.PROJECT_NAME}
        root.metadata = {'request': request[:200]}
        self._root     = root
        self._trace_id = _new_trace_id()
        self.last_trace_id  = self._trace_id
        self.last_published = False
        token = self._push(root)
        logger.info("trace %s started | session=%s customer=%s", self._trace_id, session_id, customer_id)
        try:
            yield root
        except Exception:
            root.fault = True
            raise
        finally:
            self._pop(root, token)
            self._root = None
            if sampled:
                self._publish(root)
            else:
                logger.info("trace %s not sampled (rate=%.2f)", self._trace_id, self.sampling_rate)

    @contextmanager
    def subsegment(self, name: str, namespace: Optional[str] = None,
                   fallback_ok: bool = False, metadata: Optional[dict] = None):
        """Open a subsegment under the current node. No-op outside a trace."""
        parent = self._resolve_parent()
        if parent is None:
            yield None
            return
        node = _Node(name, namespace, parent, fallback_ok)
        if metadata:
            node.metadata = metadata
        with self._lock:
            parent.children.append(node)
        token = self._push(node)
        try:
            yield node
        except Exception:
            node.fault = True
            raise
        finally:
            self._pop(node, token)

    def _publish(self, root: _Node):
        doc = root.to_doc()
        doc['trace_id'] = self._trace_id
        doc['service']  = {'version': '1.0'}
        doc['origin']   = 'AWS::AgentCore::Runtime' if os.environ.get('AGENT_RUNTIME_MODE') else None
        if not doc['origin']:
            del doc['origin']
        body = json.dumps(doc)
        if len(body) > 60_000:                      # X-Ray limit is 64 KB per document
            _strip_metadata(root)
            doc = root.to_doc(); doc['trace_id'] = self._trace_id
            body = json.dumps(doc)
        try:
            resp = self._xray().put_trace_segments(TraceSegmentDocuments=[body])
            unprocessed = resp.get('UnprocessedTraceSegments', [])
            if unprocessed:
                logger.warning("X-Ray rejected segment: %s", unprocessed)
            else:
                self.last_published = True
                logger.info("trace %s published to X-Ray (%d bytes)", self._trace_id, len(body))
        except Exception as exc:
            logger.warning("X-Ray PutTraceSegments failed: %s", exc)


def _strip_metadata(node: _Node):
    node.metadata = {}
    for c in node.children:
        _strip_metadata(c)


tracer = AgentTracer()


# ─────────────────────────────────────────────────────
# TRACED @tool DECORATOR
# ─────────────────────────────────────────────────────

def _traced(fn):
    name = fn.__name__
    node_name = _AGENT_NODE_FOR_TOOL.get(name)
    namespace = 'remote' if node_name else None
    fallback  = bool(node_name) or name in _FANOUT_TOOLS

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        logger.info("tool call  %s %s", name, _short_args(kwargs))
        with tracer.subsegment(node_name or name, namespace=namespace,
                               fallback_ok=fallback, metadata={'tool': name}):
            result = fn(*args, **kwargs)
        logger.info("tool done  %s (%.2fs)", name, time.time() - t0)
        return result
    return wrapper


def _short_args(kwargs: dict) -> str:
    parts = []
    for k, v in kwargs.items():
        text = str(v)
        if len(text) > 60:
            text = text[:60] + '...'
        parts.append(f"{k}={text!r}")
    return ' '.join(parts)


def tool(*args, **kwargs):
    """
    Drop-in replacement for `strands.tool` that also records an X-Ray
    subsegment (and an INFO log line) for every invocation.

    Supports both forms:   @tool          @tool(name=..., description=...)
    """
    if len(args) == 1 and callable(args[0]) and not kwargs:
        return _strands_tool(_traced(args[0]))

    strands_decorator = _strands_tool(*args, **kwargs)

    def decorate(fn):
        return strands_decorator(_traced(fn))
    return decorate


@contextmanager
def trace_kb_retrieval(kb_id: str):
    """Remote subsegment for one Knowledge Base retrieval (used by bedrock_kb_retrieval)."""
    label = {
        config.RETURNS_KB_ID:  'returns',
        config.SHIPPING_KB_ID: 'shipping',
        config.WARRANTY_KB_ID: 'warranty',
    }.get(kb_id) or (kb_id or 'unset')
    with tracer.subsegment(f"KnowledgeBase:{label}", namespace='remote',
                           metadata={'kb_id': kb_id}) as node:
        yield node


# ─────────────────────────────────────────────────────
# CLOUDWATCH LOGS HANDLER
# ─────────────────────────────────────────────────────

class CloudWatchLogHandler(logging.Handler):
    """Minimal CloudWatch Logs handler (no extra dependencies). Best-effort."""

    def __init__(self, log_group: str, stream_name: Optional[str] = None):
        super().__init__()
        self.log_group   = log_group
        self.stream_name = stream_name or (
            f"{os.environ.get('AGENT_RUNTIME_MODE', 'local')}/"
            f"{socket.gethostname()}/{time.strftime('%Y-%m-%d')}/{uuid.uuid4().hex[:8]}"
        )
        self._client = boto3.client('logs', region_name=config.AWS_REGION)
        self._lock   = threading.Lock()
        self._buffer = []
        self._ready  = self._ensure_stream()

    def _ensure_stream(self) -> bool:
        try:
            try:
                self._client.create_log_group(logGroupName=self.log_group)
            except self._client.exceptions.ResourceAlreadyExistsException:
                pass
            try:
                self._client.create_log_stream(logGroupName=self.log_group,
                                               logStreamName=self.stream_name)
            except self._client.exceptions.ResourceAlreadyExistsException:
                pass
            return True
        except Exception as exc:
            print(f"  [Note] CloudWatch logging disabled: {exc}", file=sys.stderr)
            return False

    def emit(self, record: logging.LogRecord) -> None:
        if not self._ready:
            return
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            self._buffer.append({'timestamp': int(record.created * 1000), 'message': msg})
            if len(self._buffer) >= 20:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buffer or not self._ready:
            return
        events, self._buffer = self._buffer, []
        events.sort(key=lambda e: e['timestamp'])
        try:
            self._client.put_log_events(logGroupName=self.log_group,
                                        logStreamName=self.stream_name,
                                        logEvents=events)
        except Exception as exc:
            self._ready = False
            print(f"  [Note] CloudWatch PutLogEvents failed: {exc}", file=sys.stderr)


_cw_handler: Optional[CloudWatchLogHandler] = None


def setup_logging(to_cloudwatch: Optional[bool] = None) -> Optional[str]:
    """
    Configure the `novamart` logger at AGENT_LOG_LEVEL (default INFO) and,
    when enabled, ship records to AGENT_LOG_GROUP in CloudWatch Logs.

    Returns the log stream name when CloudWatch shipping is active, else None.
    """
    global _cw_handler
    level_name = os.environ.get(ENV_LOG_LEVEL, 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger('novamart')
    root.setLevel(level)
    if not getattr(root, '_novamart_console', False):
        # Keep the terminal quiet: only warnings reach the console; INFO goes to CloudWatch.
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
        root.addHandler(console)
        root.propagate = False
        root._novamart_console = True

    if to_cloudwatch is None:
        to_cloudwatch = _env_flag(ENV_LOG_TO_CLOUDWATCH, False)
    if not to_cloudwatch or _cw_handler is not None:
        return _cw_handler.stream_name if _cw_handler else None

    log_group = os.environ.get(ENV_LOG_GROUP) or _safe_log_group()
    if not log_group:
        return None
    handler = CloudWatchLogHandler(log_group)
    if not handler._ready:
        return None
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root.addHandler(handler)
    _cw_handler = handler
    import atexit
    atexit.register(handler.flush)
    return handler.stream_name


def flush_logs() -> None:
    if _cw_handler is not None:
        _cw_handler.flush()


def _safe_log_group() -> str:
    try:
        return config.AGENT_LOG_GROUP
    except Exception:
        return ''


# ─────────────────────────────────────────────────────
# TASK 6 - APPLY loggingConfiguration TO AWS
# ─────────────────────────────────────────────────────

def validate_logging_configuration(logging_configuration: dict) -> None:
    """Raise ValueError if the dict does not have the expected shape."""
    if not isinstance(logging_configuration, dict):
        raise ValueError("loggingConfiguration must be a dict")
    cw = logging_configuration.get('cloudWatchConfig')
    xr = logging_configuration.get('xRayConfig')
    if not isinstance(cw, dict) or not isinstance(xr, dict):
        raise ValueError("loggingConfiguration needs 'cloudWatchConfig' and 'xRayConfig' dicts")
    for key in ('logGroupName', 'logLevel', 'enabled'):
        if key not in cw:
            raise ValueError(f"cloudWatchConfig is missing '{key}'")
    for key in ('enabled', 'samplingRate'):
        if key not in xr:
            raise ValueError(f"xRayConfig is missing '{key}'")
    rate = float(xr['samplingRate'])
    if not 0.0 <= rate <= 1.0:
        raise ValueError("xRayConfig.samplingRate must be between 0.0 and 1.0")


def enable_transaction_search(sampling_rate: float) -> dict:
    """
    Enable CloudWatch Transaction Search (X-Ray spans -> CloudWatch Logs) and
    set the indexing percentage. This is the account-level switch that
    AgentCore Observability relies on.

    X-Ray owns the reserved ``aws/spans`` namespace, so that group must be
    provisioned by enabling the CloudWatchLogs destination rather than by a
    direct CreateLogGroup call. Repair a partially enabled destination when
    necessary so a deployment produces a working Service Map.
    Every operation is idempotent.
    """
    logs = boto3.client('logs', region_name=config.AWS_REGION)
    try:
        logs.create_log_group(logGroupName=TRANSACTION_SEARCH_APPLICATION_LOG_GROUP)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

    # X-Ray is the writer, so CloudWatch Logs needs a resource-based policy in
    # addition to the permissions on the caller's IAM user or role.
    partition = boto3.session.Session().get_partition_for_region(config.AWS_REGION)
    account_id = config.ACCOUNT_ID
    policy_document = {
        'Version': '2012-10-17',
        'Statement': [{
            'Sid': 'TransactionSearchXRayAccess',
            'Effect': 'Allow',
            'Principal': {'Service': 'xray.amazonaws.com'},
            'Action': 'logs:PutLogEvents',
            'Resource': [
                f'arn:{partition}:logs:{config.AWS_REGION}:{account_id}:'
                f'log-group:{TRANSACTION_SEARCH_SPAN_LOG_GROUP}:*',
                f'arn:{partition}:logs:{config.AWS_REGION}:{account_id}:'
                f'log-group:{TRANSACTION_SEARCH_APPLICATION_LOG_GROUP}:*',
            ],
            'Condition': {
                'ArnLike': {
                    'aws:SourceArn':
                        f'arn:{partition}:xray:{config.AWS_REGION}:{account_id}:*',
                },
                'StringEquals': {'aws:SourceAccount': account_id},
            },
        }],
    }
    logs.put_resource_policy(
        policyName=TRANSACTION_SEARCH_POLICY_NAME,
        policyDocument=json.dumps(policy_document),
    )

    xray = boto3.client('xray', region_name=config.AWS_REGION)

    def span_group_exists() -> bool:
        groups = logs.describe_log_groups(
            logGroupNamePrefix=TRANSACTION_SEARCH_SPAN_LOG_GROUP,
        ).get('logGroups', [])
        return any(g.get('logGroupName') == TRANSACTION_SEARCH_SPAN_LOG_GROUP for g in groups)

    def wait_for_span_group(timeout: int = 180) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if span_group_exists():
                return
            time.sleep(5)
        raise TimeoutError(
            f"X-Ray did not provision {TRANSACTION_SEARCH_SPAN_LOG_GROUP!r} "
            f"within {timeout}s"
        )

    def update_destination_with_retry(destination: str, timeout: int = 180) -> dict:
        """Retry while an earlier asynchronous destination update is pending."""
        deadline = time.time() + timeout
        while True:
            try:
                return xray.update_trace_segment_destination(Destination=destination)
            except xray.exceptions.InvalidRequestException:
                current = xray.get_trace_segment_destination()
                if current.get('Destination') == destination:
                    return current
                if time.time() >= deadline:
                    raise
                time.sleep(5)

    dest = xray.get_trace_segment_destination()
    if dest.get('Destination') == 'CloudWatchLogs' and not span_group_exists():
        # A previous incomplete setup can leave the destination ACTIVE without
        # its AWS-owned group. Toggle it so X-Ray provisions aws/spans itself.
        update_destination_with_retry('XRay')
        update_destination_with_retry('CloudWatchLogs')
        wait_for_span_group()
        dest = xray.get_trace_segment_destination()
    elif dest.get('Destination') != 'CloudWatchLogs':
        update_destination_with_retry('CloudWatchLogs')
        wait_for_span_group()
        dest = xray.get_trace_segment_destination()
    pct = int(round(sampling_rate * 100))
    xray.update_indexing_rule(Name='Default', Rule={'Probabilistic': {'DesiredSamplingPercentage': pct}})
    return {'destination': dest.get('Destination'), 'status': dest.get('Status'), 'indexing_percent': pct}


def wait_for_runtime_ready(agentcore_control, runtime_id: str, timeout: int = 300) -> str:
    """Poll get_agent_runtime until status is READY (or a failure state)."""
    deadline = time.time() + timeout
    status = 'UNKNOWN'
    while time.time() < deadline:
        status = agentcore_control.get_agent_runtime(agentRuntimeId=runtime_id)['status']
        if status == 'READY':
            return status
        if 'FAIL' in status or status in ('DELETING', 'DELETE_FAILED'):
            raise RuntimeError(f"AgentCore Runtime {runtime_id} entered status {status}")
        print('.', end='', flush=True)
        time.sleep(10)
    raise TimeoutError(f"AgentCore Runtime {runtime_id} still {status} after {timeout}s")


def apply_observability_config(runtime_arn: str, logging_configuration: dict) -> dict:
    """
    Apply loggingConfiguration to AWS resources.

      cloudWatchConfig -> runtime env: AGENT_LOG_GROUP, AGENT_LOG_LEVEL,
                          AGENT_LOG_TO_CLOUDWATCH (and the log group is created)
      xRayConfig       -> CloudWatch Transaction Search + indexing percentage,
                          runtime env: AGENT_TRACING_ENABLED, AGENT_TRACE_SAMPLING_RATE

    The runtime environment variables are written to agentcore/agentcore.json
    and applied with the AgentCore CLI (`agentcore deploy -y`) - the runtime
    is CDK-managed by the CLI, so it is never changed behind the CLI's back.

    Returns a summary dict. Raises on failure - nothing is faked.
    """
    validate_logging_configuration(logging_configuration)
    cw = logging_configuration['cloudWatchConfig']
    xr = logging_configuration['xRayConfig']
    summary = {}

    # 1. CloudWatch log group
    logs = boto3.client('logs', region_name=config.AWS_REGION)
    try:
        logs.create_log_group(logGroupName=cw['logGroupName'])
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    summary['log_group'] = cw['logGroupName']

    # 2. X-Ray / Transaction Search
    if xr['enabled']:
        summary['xray'] = enable_transaction_search(float(xr['samplingRate']))

    # 3. Runtime environment variables - declared in agentcore/agentcore.json
    #    and applied by the AgentCore CLI (`agentcore deploy -y`), so the
    #    deployed runtime always matches the project configuration.
    import agentcore_cli
    agentcore_control = boto3.client('bedrock-agentcore-control', region_name=config.AWS_REGION)
    runtime_id = runtime_arn.split('/')[-1]
    env = {}
    # Refresh Knowledge Base IDs when re-running deploy after the KBs are created.
    for key in ('RETURNS_KB_ID', 'SHIPPING_KB_ID', 'WARRANTY_KB_ID'):
        value = getattr(config, key, '')
        if value:
            env[key] = value
    env.update({
        ENV_LOG_GROUP:         cw['logGroupName'],
        ENV_LOG_LEVEL:         str(cw['logLevel']).upper(),
        ENV_LOG_TO_CLOUDWATCH: 'true' if cw['enabled'] else 'false',
        ENV_TRACING_ENABLED:   'true' if xr['enabled'] else 'false',
        ENV_SAMPLING_RATE:     str(float(xr['samplingRate'])),
    })
    agentcore_cli.configure_runtime(env_vars=env)
    print("  Runtime environment written to agentcore/agentcore.json - applying with the AgentCore CLI")
    agentcore_cli.deploy()
    print("  Waiting for runtime status READY", end='', flush=True)
    wait_for_runtime_ready(agentcore_control, runtime_id)
    print(' ready.')
    env = agentcore_cli.runtime_env_vars()
    summary['runtime_env'] = {k: env[k] for k in (ENV_LOG_GROUP, ENV_LOG_LEVEL, ENV_LOG_TO_CLOUDWATCH,
                                                 ENV_TRACING_ENABLED, ENV_SAMPLING_RATE)}
    return summary


def print_trace_hint() -> None:
    """Print the Service Map location after a traced local run."""
    if tracer.last_trace_id and tracer.last_published:
        service_map_url = (
            f"https://console.aws.amazon.com/cloudwatch/home?region={config.AWS_REGION}"
            "#xray:service-map/map"
        )
        print(f"\n  X-Ray trace {tracer.last_trace_id} published successfully.")
        print("  Allow 30-60 seconds, then open the Service Map and select "
              "'Last 5 minutes':")
        print(f"  {service_map_url}")
        print("  Submission step: take a screenshot showing the full "
              "NovaMart-Orchestrator → worker-agent call chain.")
    elif tracer.last_trace_id:
        print(f"\n  X-Ray trace {tracer.last_trace_id} was NOT published - see the warning above.")
