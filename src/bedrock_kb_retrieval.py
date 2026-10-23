"""
bedrock_kb_retrieval.py
=======================
Bedrock Knowledge Base retrieval utility.

This module provides a thin wrapper around the Bedrock Agent Runtime
`retrieve()` API. It is used by the three retriever sub-agents inside
PolicyAgent:

    ReturnsPolicyRetrieverAgent   → RETURNS_KB_ID
    ShippingPolicyRetrieverAgent  → SHIPPING_KB_ID
    WarrantyPolicyRetrieverAgent  → WARRANTY_KB_ID

Why Bedrock Knowledge Bases instead of a custom RAG pipeline?
  - Managed embeddings (Titan Embed Text v2) - no manual chunking or indexing
  - S3 Vectors as the backing store - cheap, no OpenSearch cluster required
  - bedrock-agent-runtime.retrieve() is the idiomatic AWS pattern for
    grounding agents in document corpora

API reference:
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html
"""

import boto3
import os
import sys

# Each retrieval becomes a KnowledgeBase:<domain>
# node on the Service Map (see agent_observability.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_observability import trace_kb_retrieval

# Bedrock Agent Runtime client  (handles KB retrieval - different from bedrock-runtime
# which handles model invocation)
_bedrock_agent_runtime = boto3.client(
    'bedrock-agent-runtime',
    region_name=os.environ.get('AWS_REGION', 'us-east-1')
)


def retrieve_from_knowledge_base(
    kb_id: str,
    query: str,
    top_k: int = 3
) -> list[dict]:
    """
    Retrieve the top-k most relevant document chunks from a Bedrock Knowledge Base.

    Args:
        kb_id:  The Knowledge Base ID (e.g. "ABCD1234EF").
                Use config.RETURNS_KB_ID / SHIPPING_KB_ID / WARRANTY_KB_ID.
        query:  Natural-language question to retrieve context for.
        top_k:  Number of results to return (default: 3).

    Returns:
        List of result dicts, each containing:
            {
                'text':   '<retrieved passage>',
                'source': '<S3 URI of the source document>',
                'score':  <float relevance score>
            }
        Sorted by score descending. Returns an empty list if kb_id is blank
        (allows graceful degradation when a KB hasn't been created yet).
    """
    if not kb_id:
        return []

    with trace_kb_retrieval(kb_id):
        try:
            response = _bedrock_agent_runtime.retrieve(
                knowledgeBaseId=kb_id,
                retrievalQuery={'text': query},
                retrievalConfiguration={
                    'vectorSearchConfiguration': {
                        'numberOfResults': top_k
                    }
                }
            )
        except Exception as exc:
            # Surface the error as structured text so the calling agent can report it
            return [{
                'text':   f"Knowledge base retrieval failed: {exc}",
                'source': 'error',
                'score':  0.0
            }]

    results = []
    for item in response.get('retrievalResults', []):
        content = item.get('content', {})
        location = item.get('location', {})
        score    = item.get('score', 0.0)

        # Extract text - KB returns either plain text or a structured object
        text = content.get('text', '')

        # Extract S3 URI from location metadata
        s3_location = location.get('s3Location', {})
        source = s3_location.get('uri', 'unknown')

        results.append({
            'text':   text,
            'source': source,
            'score':  round(float(score), 4)
        })

    # Already sorted by Bedrock, but sort again to be explicit
    results.sort(key=lambda x: x['score'], reverse=True)
    return results


def format_kb_results(results: list[dict]) -> str:
    """
    Format Knowledge Base results into a readable string for an agent's context.

    Args:
        results: Output of retrieve_from_knowledge_base()

    Returns:
        Formatted string suitable for inclusion in an agent prompt.
    """
    if not results:
        return "No relevant policy documents found."

    formatted = []
    for i, r in enumerate(results, 1):
        formatted.append(
            f"[Passage {i} | Score: {r['score']:.3f}]\n"
            f"{r['text']}\n"
            f"Source: {r['source']}"
        )
    return "\n\n".join(formatted)
