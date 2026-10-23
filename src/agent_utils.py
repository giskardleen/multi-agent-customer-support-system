"""
agent_utils.py
==============
Terminal trace UI and output utilities for the multi-agent system.

This module provides:
  - _C            : ANSI colour constants (one colour per agent)
  - _TraceWriter  : stdout proxy that reformats Strands SDK output line-by-line
  - AgentTrace    : structured step-by-step trace with timing and DynamoDB summary
  - _AGENT_META   : display metadata (label, colour, role) per specialist agent
  - _strip_xml_tags : strips LLM-internal XML scaffolding from response strings

These utilities are kept separate from the agent architecture.
"""

import sys
import os
import re
import io
import threading
import time

# Ensure parent directory is on sys.path so config.py is importable
# regardless of where this module is imported from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


# ─────────────────────────────────────────────────────
# ANSI COLOUR SUPPORT
# ─────────────────────────────────────────────────────

def _enable_windows_ansi() -> None:
    """Enable ANSI VT-processing in the Windows console (no-op elsewhere)."""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            pass

_enable_windows_ansi()
_COLOUR_ON = sys.stdout.isatty() or bool(os.environ.get('FORCE_COLOR'))

def _ansi(code: str) -> str:
    return f'\033[{code}m' if _COLOUR_ON else ''


class _C:
    """ANSI colour constants - one colour per agent for easy visual scanning."""
    RESET  = _ansi('0')
    BOLD   = _ansi('1')
    DIM    = _ansi('2')
    # Per-agent colours (regular, not bright - more professional tone)
    ORCH   = _ansi('36')   # cyan        - Orchestrator
    INV    = _ansi('33')   # yellow      - InventoryAgent
    POL    = _ansi('34')   # blue        - PolicyAgent
    REF    = _ansi('35')   # magenta     - RefundAgent
    COM    = _ansi('32')   # green       - CommunicationAgent
    KB     = _ansi('34')   # blue        - KB sub-agents
    # Status colours
    OK     = _ansi('32')   # green  - success
    ERR    = _ansi('31')   # red    - error
    GRY    = _ansi('37')   # grey   - secondary / structural text
    # Layout constant
    W      = 68             # content width (excludes leading 2-space indent)


def _strip_xml_tags(text: str) -> str:
    """
    Strip LLM-internal XML scaffolding from the final response string.
    If a <result> block is present, extracts only its content.
    Otherwise removes known internal scaffolding tags and returns clean text.
    """
    result_match = re.search(r'<result>(.*?)</result>', text, re.DOTALL)
    if result_match:
        return result_match.group(1).strip()
    # Remove known internal scaffolding block tags and their content
    text = re.sub(
        r'<(search_quality_reflection|search_quality_score)>.*?</\1>',
        '', text, flags=re.DOTALL
    )
    # Remove any remaining lone XML-style tags
    text = re.sub(r'</?[a-zA-Z_][a-zA-Z0-9_]*>', '', text)
    return text.strip()


# ── Real stdout saved before any proxy is installed ───────────────────────────
_real_stdout = sys.stdout


def _trace_print(*args, **kwargs) -> None:
    """
    Print directly to the real stdout, bypassing the _TraceWriter proxy.
    All AgentTrace methods use this so our structured headers are never
    double-processed by the proxy's line-rewriting logic.
    """
    kwargs.setdefault('file', _real_stdout)
    print(*args, **kwargs)


# ─────────────────────────────────────────────────────
# STDOUT PROXY
# ─────────────────────────────────────────────────────

class _TraceWriter:
    """
    Stdout proxy installed during orchestrator() calls.

    Two responsibilities:
      1. Reformat Strands SDK output line by line:
            "Tool #N: tool_name"  ->  [TOOL CALL]  tool_name
            all other text        ->  | <text>      (agent reasoning)
         XML scaffolding tags are stripped before display so LLM-internal
         markup never leaks into the trace.

      2. Global parallel-suppression via _suppress_parallel (threading.Event).
         Set by AgentTrace.kb_start() before ThreadPoolExecutor runs;
         cleared by AgentTrace.kb_done() after all futures join.
         Any write() call while the flag is set is silently discarded -
         this covers ALL threads including Strands SDK's internal streaming
         child threads which do not inherit thread-local variables.
         Results are printed cleanly and sequentially via trace.kb_result()
         after suppression is lifted, so learners see ordered output.
    """

    _TOOL_PAT = re.compile(r'^Tool\s*#\d+:\s*(.+)$')
    _XML_TAG  = re.compile(r'</?[a-zA-Z_][a-zA-Z0-9_]*>')

    # Class-level event - shared across all threads; set/cleared by AgentTrace
    _suppress_parallel: threading.Event = threading.Event()

    def __init__(self, real: 'io.TextIOBase') -> None:
        self._real = real
        self._buf  = ''    # incomplete-line accumulator (main thread only)

    # ── io.TextIOBase interface ────────────────────────────────────────────

    def write(self, text: str) -> int:
        if self._suppress_parallel.is_set():
            return len(text)     # discard ALL output during parallel retrieval
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._emit(line)
        return len(text)

    def flush(self) -> None:
        if self._suppress_parallel.is_set():
            return               # nothing buffered to flush during suppression
        if self._buf:
            self._emit(self._buf)
            self._buf = ''
        self._real.flush()

    def isatty(self) -> bool:
        return self._real.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._real, 'encoding', 'utf-8')

    @property
    def errors(self) -> str:
        return getattr(self._real, 'errors', 'strict')

    # ── Line transformation ────────────────────────────────────────────────

    def _emit(self, line: str) -> None:
        s = self._XML_TAG.sub('', line).strip()
        if not s:
            return
        m = self._TOOL_PAT.match(s)
        if m:
            tool = m.group(1).strip()
            self._real.write(
                f"  {_C.GRY}  [TOOL CALL]  "
                f"{_C.RESET}{_C.BOLD}{tool}{_C.RESET}\n"
            )
        else:
            self._real.write(
                f"  {_C.DIM}  | {s}{_C.RESET}\n"
            )


# Proxy writer instance - installed/uninstalled by the chat loop and demo.py
# around each orchestrator() call.
_trace_writer = _TraceWriter(_real_stdout)


# ─────────────────────────────────────────────────────
# AGENT METADATA
# ─────────────────────────────────────────────────────

_AGENT_META: dict = {
    'inventory_agent': (
        '[INV]', 'INVENTORY AGENT', _C.INV,
        'Fetch order details and customer tier from DynamoDB',
    ),
    'policy_agent': (
        '[POL]', 'POLICY AGENT', _C.POL,
        'Multi-Agent RAG -- parallel KB retrieval + synthesis',
    ),
    'refund_agent': (
        '[REF]', 'REFUND AGENT', _C.REF,
        'Evaluate return eligibility using inventory and policy data',
    ),
    'communication_agent': (
        '[COM]', 'COMMUNICATION AGENT', _C.COM,
        'Compose the final customer-facing response',
    ),
}

# Reason shown in workflow summary when an agent column was not populated
_AGENT_SKIP_REASON: dict = {
    'inventory_agent':     'not required for this request type',
    'policy_agent':        'not required for this request type',
    'refund_agent':        'not required for this request type',
    'communication_agent': 'not required for this request type',
}


# ─────────────────────────────────────────────────────
# AGENT TRACE
# ─────────────────────────────────────────────────────

class AgentTrace:
    """
    Structured tracing layer for educational terminal output.

    All methods write directly to _real_stdout via _trace_print() so that
    our formatted headers are never processed by the _TraceWriter proxy.

    Usage flow:
      1. chat loop / demo.py    -> trace.new_turn()       before orchestrator()
      2. route_to_*()           -> trace.step_start()     before specialist agent
      3. route_to_*()           -> trace.agent_section()  labels agent reasoning
      4. search_all_policies()  -> trace.kb_start()       before ThreadPoolExecutor
      5. search_all_policies()  -> trace.kb_done()        after  ThreadPoolExecutor
      6. search_all_policies()  -> trace.kb_result()      per sub-agent, sequentially
      7. route_to_*()           -> trace.step_done()      after _update_workflow_state()
      8. chat loop / demo.py    -> trace.summary()        after orchestrator() returns

    The read_state_fn parameter is injected by agent_orchestrator.py after
    _read_workflow_state is defined, avoiding a circular import.
    """

    def __init__(self, read_state_fn=None) -> None:
        self._step       = 0
        self._t0_turn    = 0.0
        self._t0_step    = 0.0
        self._t0_kb      = 0.0
        self._read_state = read_state_fn   # callable: session_id -> dict

    # ── called by chat loop / demo.py ──────────────────────────────────────

    def new_turn(self) -> None:
        """Reset step counter and print the orchestrator header for a new request."""
        self._step    = 0
        self._t0_turn = time.time()
        w = _C.W
        _trace_print()
        _trace_print(f"  {_C.GRY}{'=' * w}{_C.RESET}")
        _trace_print(f"  {_C.ORCH}{_C.BOLD}[ORCHESTRATOR]{_C.RESET}  "
                     f"{_C.GRY}Routing request to specialist agents...{_C.RESET}")
        _trace_print(f"  {_C.GRY}Model : {config.ORCHESTRATOR_MODEL_ID}{_C.RESET}")
        _trace_print(f"  {_C.GRY}{'=' * w}{_C.RESET}")
        _trace_print(f"  {_C.GRY}  [AGENT REASONING - ORCHESTRATOR]{_C.RESET}")

    def summary(self, session_id: str, elapsed: float) -> None:
        """
        Print the DynamoDB WorkflowState after all agents have run.
        Shows which columns each agent populated, with a reason for skipped agents.
        """
        state = (self._read_state(session_id) if self._read_state else {}) or {}
        w     = _C.W
        agent_cols = [
            'inventory_agent', 'policy_agent',
            'refund_agent',    'communication_agent',
        ]
        _trace_print()
        _trace_print(f"  {_C.GRY}{'=' * w}{_C.RESET}")
        _trace_print(f"  {_C.BOLD}WORKFLOW STATE SUMMARY{_C.RESET}  "
                     f"{_C.GRY}DynamoDB: {config.WORKFLOW_STATE_TABLE}{_C.RESET}")
        _trace_print(f"  {_C.GRY}{'=' * w}{_C.RESET}")
        for key in ('session_id', 'customer_id', 'version'):
            _trace_print(f"  {_C.GRY}{key:<20}{_C.RESET} {state.get(key, '--')}")
        _trace_print()
        _trace_print(f"  {_C.GRY}{'Agent':<28}  {'State':<14}  Note{_C.RESET}")
        _trace_print(f"  {_C.GRY}{'─' * 26}  {'─' * 12}  {'─' * 20}{_C.RESET}")
        for col in agent_cols:
            _, label, colour, _ = _AGENT_META[col]
            if col in state:
                badge = f"{_C.OK}[POPULATED]{_C.RESET}"
                note  = f"{_C.GRY}result stored in DynamoDB{_C.RESET}"
            else:
                badge = f"{_C.GRY}[NOT SET]  {_C.RESET}"
                note  = f"{_C.DIM}{_AGENT_SKIP_REASON.get(col, '')}{_C.RESET}"
            _trace_print(f"  {colour}{col:<28}{_C.RESET}  {badge}  {note}")
        _trace_print()
        _trace_print(f"  {_C.GRY}Total elapsed : {elapsed:.1f}s{_C.RESET}")
        _trace_print(f"  {_C.GRY}{'=' * w}{_C.RESET}")

    # ── called by routing tools inside build_orchestrator_agent() ──────────

    def step_start(self, column: str) -> None:
        """
        Print a numbered step banner before a specialist agent is invoked.
        Auto-increments so learners see the sequential routing order.
        """
        self._step   += 1
        self._t0_step = time.time()
        _, label, colour, role = _AGENT_META.get(
            column, ('[AGT]', column.upper(), _C.GRY, ''))
        n = self._step
        w = _C.W
        _trace_print()
        _trace_print(f"  {_C.GRY}{'─' * w}{_C.RESET}")
        _trace_print(f"  {_C.GRY}[ STEP {n:02d} ]{_C.RESET}  "
                     f"{colour}{_C.BOLD}{label}{_C.RESET}")
        _trace_print(f"  {_C.GRY}           Role  : {role}{_C.RESET}")
        _trace_print(f"  {_C.GRY}           Model : {config.WORKER_MODEL_ID}{_C.RESET}")
        _trace_print(f"  {_C.GRY}{'─' * w}{_C.RESET}")

    def step_done(self, column: str, old_version: int) -> None:
        """Print timing and DynamoDB version bump after a specialist agent returns."""
        elapsed = time.time() - self._t0_step
        _, _, colour, _ = _AGENT_META.get(column, ('', '', _C.GRY, ''))
        new_v   = old_version + 1
        _trace_print(f"  {_C.GRY}           Status : {_C.OK}[COMPLETE]{_C.RESET}  "
                     f"{_C.GRY}Elapsed: {elapsed:.1f}s  "
                     f"(LLM may still stream final text below){_C.RESET}")
        _trace_print(f"  {_C.GRY}           State  : "
                     f"DynamoDB[{colour}{column}{_C.RESET}{_C.GRY}]  "
                     f"version {old_version} -> {new_v}{_C.RESET}")

    def agent_section(self, label: str) -> None:
        """
        Print a labelled [AGENT REASONING] header immediately before a
        specialist agent runs.  Learners can clearly identify which agent's
        LLM output (reasoning + tool calls) follows below it.
        """
        _trace_print(f"  {_C.GRY}  [AGENT REASONING - {label}]{_C.RESET}")

    # ── called inside search_all_policies() in build_policy_agent() ────────

    def kb_start(self, kb_map: dict) -> None:
        """
        Print the parallel-retrieval banner inside PolicyAgent.
        kb_map = {'Returns': KB_ID, 'Shipping': KB_ID, 'Warranty': KB_ID}
        Makes ThreadPoolExecutor parallel execution visible to learners.

        Sets _TraceWriter._suppress_parallel so all stdout from concurrent
        sub-agent threads is discarded.  Results are printed sequentially
        by kb_result() after kb_done() clears the flag.
        """
        self._t0_kb = time.time()
        entries     = list(kb_map.items())
        names = {
            'Returns':  ('SUB-AGENT 01', 'ReturnsRetriever '),
            'Shipping': ('SUB-AGENT 02', 'ShippingRetriever'),
            'Warranty': ('SUB-AGENT 03', 'WarrantyRetriever'),
        }
        _trace_print(f"  {_C.GRY}  [PARALLEL RETRIEVAL - START]  "
                     f"Spawning {len(entries)} sub-agents concurrently "
                     f"via ThreadPoolExecutor{_C.RESET}")
        _trace_print(f"  {_C.GRY}  {'─' * 54}{_C.RESET}")
        for i, (domain, kb_id) in enumerate(entries, 1):
            num, lbl = names.get(domain, (f'SUB-AGENT {i:02d}', domain))
            _trace_print(f"  {_C.GRY}  +-- [{num}]  "
                         f"{_C.KB}{lbl}{_C.RESET}  "
                         f"{_C.GRY}KB-ID: {kb_id}{_C.RESET}")
        _trace_print(f"  {_C.DIM}  Retrieving in parallel... "
                     f"(sub-agent output suppressed - "
                     f"clean results printed sequentially after [END]){_C.RESET}")
        _TraceWriter._suppress_parallel.set()

    def kb_done(self, count: int) -> None:
        """
        Clear suppression and print retrieval-complete banner after all threads join.
        """
        _TraceWriter._suppress_parallel.clear()
        elapsed = time.time() - self._t0_kb
        _trace_print(f"  {_C.GRY}  {'─' * 54}{_C.RESET}")
        _trace_print(f"  {_C.GRY}  [PARALLEL RETRIEVAL - END]  "
                     f"{_C.OK}All {count} KBs responded{_C.RESET}  "
                     f"{_C.GRY}Elapsed: {elapsed:.1f}s{_C.RESET}")

    def kb_result(self, domain: str, content: str) -> None:
        """
        Print one sub-agent's retrieved KB content sequentially.
        Called once per sub-agent AFTER all threads complete, so output
        is always clean and ordered regardless of thread completion order.
        """
        names = {
            'Returns':  ('SUB-AGENT 01', 'ReturnsRetriever ', config.RETURNS_KB_ID),
            'Shipping': ('SUB-AGENT 02', 'ShippingRetriever', config.SHIPPING_KB_ID),
            'Warranty': ('SUB-AGENT 03', 'WarrantyRetriever', config.WARRANTY_KB_ID),
        }
        num, label, kb_id = names.get(domain, ('SUB-AGENT', domain, '?'))
        _trace_print()
        _trace_print(f"  {_C.GRY}  [{num}]  "
                     f"{_C.KB}{label}{_C.RESET}  "
                     f"{_C.GRY}KB: {kb_id}{_C.RESET}")
        for line in content.splitlines():
            if line.strip():
                _trace_print(f"  {_C.DIM}    | {line}{_C.RESET}")
