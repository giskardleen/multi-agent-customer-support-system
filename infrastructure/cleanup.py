"""
cleanup.py
==========
Remove EVERYTHING the NovaMart project created in the AWS account, in
dependency order, so the account is back to its pre-project state:

  1. Bedrock Knowledge Bases (+ data sources) and the service roles/policies the
     console wizard created for them  (AmazonBedrockExecutionRoleForKnowledgeBase_*)
  2. The AgentCore CLI stack that holds the AgentCore Runtime
     (AgentCore-udacity-default, created by `agentcore deploy`), Memory,
     workload identity (and the optional Gateway). The shared CDK bootstrap
     stack (CDKToolkit) is left in place.
  3. Bedrock Guardrail (all versions)
  4. S3 policy bucket contents (all object versions + delete markers)
  5. The CloudFormation stack (DynamoDB tables, S3 bucket, S3 Vectors bucket +
     indexes, IAM execution role, log group) - waits for DELETE_COMPLETE
  6. Anything with the project prefix left behind if the stack was already gone
     (tables, buckets, vector buckets/indexes, role, log groups), plus the
     AgentCore runtime log groups
  7. Optionally (--disable-transaction-search) turn CloudWatch Transaction Search
     back off and remove the X-Ray resource policy

Usage (from the project root, same credentials/region as the project):

    python infrastructure/cleanup.py            # dry run - prints what would be deleted
    python infrastructure/cleanup.py --yes      # delete
    python infrastructure/cleanup.py --yes --disable-transaction-search

Region and names come from config.py / .env exactly as the project uses them.
Every step tolerates "already gone" and permission errors and keeps going, so
the script can be re-run until it reports nothing left.
"""

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

REGION  = config.AWS_REGION
PREFIX  = config.PROJECT_NAME                     # udacity-agentcore
KB_NAME_PREFIX = 'novamart-'                      # novamart-returns-policy-kb, ...
TS_POLICY_NAME = 'NovaMartTransactionSearchXRayAccess'

DRY = True
FOUND = 0


def say(msg):
    print(f"  {msg}")


def act(label, fn):
    """Run one delete step unless dry-run; never abort the whole cleanup."""
    global FOUND
    FOUND += 1
    if DRY:
        say(f"[would delete] {label}")
        return True
    try:
        fn()
        say(f"[deleted] {label}")
        return True
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '')
        if code in ('ResourceNotFoundException', 'NoSuchEntity', 'NoSuchBucket',
                    'NotFoundException', 'ValidationError'):
            say(f"[already gone] {label}")
        else:
            say(f"[FAILED] {label}: {code} {exc.response.get('Error', {}).get('Message', '')}")
        return False
    except Exception as exc:                                    # noqa: BLE001
        say(f"[FAILED] {label}: {exc}")
        return False


def safe(fn, default):
    try:
        return fn()
    except Exception:                                           # noqa: BLE001
        return default


# ─────────────────────────── 1. Knowledge Bases ───────────────────────────

def cleanup_knowledge_bases():
    print("\n1. Bedrock Knowledge Bases")
    agent = boto3.client('bedrock-agent', region_name=REGION)
    iam   = boto3.client('iam')
    wanted_ids = {v for v in (config.RETURNS_KB_ID, config.SHIPPING_KB_ID, config.WARRANTY_KB_ID) if v}
    kbs = []
    for page in safe(lambda: list(agent.get_paginator('list_knowledge_bases').paginate()), []):
        for kb in page.get('knowledgeBaseSummaries', []):
            if kb['knowledgeBaseId'] in wanted_ids or kb['name'].startswith(KB_NAME_PREFIX):
                kbs.append(kb)
    if not kbs:
        say("none found")
        return
    roles = set()
    for kb in kbs:
        kb_id = kb['knowledgeBaseId']
        detail = safe(lambda: agent.get_knowledge_base(knowledgeBaseId=kb_id)['knowledgeBase'], {})
        if detail.get('roleArn'):
            roles.add(detail['roleArn'].split('/')[-1])
        for ds in safe(lambda: agent.list_data_sources(knowledgeBaseId=kb_id)['dataSourceSummaries'], []):
            act(f"data source {ds['name']} ({ds['dataSourceId']}) of KB {kb['name']}",
                lambda ds=ds: agent.delete_data_source(knowledgeBaseId=kb_id, dataSourceId=ds['dataSourceId']))
        act(f"knowledge base {kb['name']} ({kb_id})",
            lambda kb_id=kb_id: agent.delete_knowledge_base(knowledgeBaseId=kb_id))
    # Wait for the KBs to disappear before touching their roles / vector indexes
    if not DRY:
        deadline = time.time() + 180
        while time.time() < deadline:
            left = [kb for kb in kbs if safe(lambda kb=kb: agent.get_knowledge_base(
                knowledgeBaseId=kb['knowledgeBaseId']) and True, False)]
            if not left:
                break
            time.sleep(5)
    for role in sorted(roles):
        if not role.startswith('AmazonBedrockExecutionRoleForKnowledgeBase'):
            say(f"[kept] role {role} (not a console-created KB role)")
            continue
        for pol in safe(lambda: iam.list_attached_role_policies(RoleName=role)['AttachedPolicies'], []):
            arn = pol['PolicyArn']
            act(f"detach {pol['PolicyName']} from {role}",
                lambda arn=arn: iam.detach_role_policy(RoleName=role, PolicyArn=arn))
            if ':aws:policy/' not in arn and 'AmazonBedrock' in pol['PolicyName']:
                act(f"policy {pol['PolicyName']}", lambda arn=arn: _delete_policy(iam, arn))
        for name in safe(lambda: iam.list_role_policies(RoleName=role)['PolicyNames'], []):
            act(f"inline policy {name} of {role}",
                lambda name=name: iam.delete_role_policy(RoleName=role, PolicyName=name))
        act(f"role {role}", lambda role=role: iam.delete_role(RoleName=role))


def _delete_policy(iam, arn):
    for v in iam.list_policy_versions(PolicyArn=arn)['Versions']:
        if not v['IsDefaultVersion']:
            iam.delete_policy_version(PolicyArn=arn, VersionId=v['VersionId'])
    iam.delete_policy(PolicyArn=arn)


# ─────────────────────────── 2. AgentCore ───────────────────────────

def wait_for_stack_gone(cf, name, timeout=900):
    print("  waiting for DELETE_COMPLETE", end='', flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st = cf.describe_stacks(StackName=name)['Stacks'][0]['StackStatus']
        except ClientError:
            print(' done.')
            return
        if st == 'DELETE_FAILED':
            print(' DELETE_FAILED')
            for ev in cf.describe_stack_events(StackName=name)['StackEvents'][:20]:
                if ev.get('ResourceStatus') == 'DELETE_FAILED':
                    say(f"    {ev['LogicalResourceId']}: {ev.get('ResourceStatusReason', '')}")
            raise RuntimeError("stack deletion failed - see reasons above; fix and re-run")
        print('.', end='', flush=True)
        time.sleep(10)
    raise TimeoutError("stack still deleting after 15 min")


def cleanup_agentcore():
    print("\n2. AgentCore runtime (AgentCore CLI stack), memory, workload identity, gateway")
    ctl = boto3.client('bedrock-agentcore-control', region_name=REGION)
    cf  = boto3.client('cloudformation', region_name=REGION)
    runtime_ids = [rt['agentRuntimeId']
                   for rt in safe(lambda: ctl.list_agent_runtimes().get('agentRuntimes', []), [])
                   if rt['agentRuntimeName'] == config.AGENTCORE_RUNTIME_NAME]

    # The runtime was created by `agentcore deploy` (AgentCore CLI) as a CDK /
    # CloudFormation stack - delete the stack so CloudFormation removes the
    # runtime cleanly, then remove anything with the runtime name left behind.
    cli_stack = config.AGENTCORE_STACK_NAME
    try:
        status = cf.describe_stacks(StackName=cli_stack)['Stacks'][0]['StackStatus']
        say(f"stack {cli_stack} is {status}")

        def _delete_cli_stack():
            cf.delete_stack(StackName=cli_stack)
            wait_for_stack_gone(cf, cli_stack)
            # forget the deployment locally so a later `agentcore deploy` starts clean
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))
            import agentcore_cli
            agentcore_cli.reset_deployed_state()
        act(f"AgentCore CLI stack {cli_stack} (runtime {config.AGENTCORE_RUNTIME_NAME})", _delete_cli_stack)
    except ClientError:
        say(f"stack {cli_stack} does not exist (nothing deployed with the AgentCore CLI)")

    for rt in safe(lambda: ctl.list_agent_runtimes().get('agentRuntimes', []), []):
        if rt['agentRuntimeName'] == config.AGENTCORE_RUNTIME_NAME:
            act(f"agent runtime {rt['agentRuntimeName']} ({rt['agentRuntimeId']}) left outside the stack",
                lambda rid=rt['agentRuntimeId']: ctl.delete_agent_runtime(agentRuntimeId=rid))
    for m in safe(lambda: ctl.list_memories().get('memories', []), []):
        if m['id'].startswith(config.MEMORY_NAME):
            act(f"memory {m['id']}", lambda mid=m['id']: ctl.delete_memory(memoryId=mid))
    for wi in safe(lambda: ctl.list_workload_identities().get('workloadIdentities', []), []):
        if any(rid in wi['name'] for rid in runtime_ids) or config.AGENTCORE_RUNTIME_NAME in wi['name']:
            act(f"workload identity {wi['name']}",
                lambda n=wi['name']: ctl.delete_workload_identity(name=n))
    for gw in safe(lambda: ctl.list_gateways().get('items', []), []):
        if gw['name'].startswith('novamart-support-'):
            gid = gw['gatewayId']
            for t in safe(lambda: ctl.list_gateway_targets(gatewayIdentifier=gid).get('items', []), []):
                act(f"gateway target {t['name']}",
                    lambda t=t: ctl.delete_gateway_target(gatewayIdentifier=gid, targetId=t['targetId']))
            act(f"gateway {gw['name']}", lambda gid=gid: ctl.delete_gateway(gatewayIdentifier=gid))
    if not runtime_ids:
        say("no runtime named " + config.AGENTCORE_RUNTIME_NAME)
    return runtime_ids


# ─────────────────────────── 3. Guardrail ───────────────────────────

def cleanup_guardrail():
    print("\n3. Bedrock Guardrail")
    bedrock = boto3.client('bedrock', region_name=REGION)
    found = False
    for g in safe(lambda: bedrock.list_guardrails().get('guardrails', []), []):
        if g['name'] == config.GUARDRAIL_NAME:
            found = True
            act(f"guardrail {g['name']} ({g['id']}) incl. all versions",
                lambda gid=g['id']: bedrock.delete_guardrail(guardrailIdentifier=gid))
    if not found:
        say("none found")


# ─────────────────────────── 4./6. S3 + S3 Vectors ───────────────────────────

def empty_bucket(s3, bucket):
    """Delete every object version and delete marker (bucket is versioned)."""
    paginator = s3.get_paginator('list_object_versions')
    for page in paginator.paginate(Bucket=bucket):
        objs = [{'Key': o['Key'], 'VersionId': o['VersionId']}
                for key in ('Versions', 'DeleteMarkers') for o in page.get(key, [])]
        for i in range(0, len(objs), 1000):
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objs[i:i + 1000], 'Quiet': True})


def project_buckets(s3):
    return [b['Name'] for b in safe(lambda: s3.list_buckets()['Buckets'], [])
            if b['Name'].startswith(f"{PREFIX}-")]


def cleanup_bucket_contents():
    print("\n4. S3 policy bucket contents")
    s3 = boto3.client('s3', region_name=REGION)
    names = project_buckets(s3)
    if not names:
        say("none found")
    for b in names:
        act(f"all objects/versions in s3://{b}", lambda b=b: empty_bucket(s3, b))


def cleanup_vector_buckets():
    s3v = boto3.client('s3vectors', region_name=REGION)
    for vb in safe(lambda: s3v.list_vector_buckets().get('vectorBuckets', []), []):
        name = vb['vectorBucketName']
        if not name.startswith(f"{PREFIX}-"):
            continue
        for idx in safe(lambda: s3v.list_indexes(vectorBucketName=name).get('indexes', []), []):
            act(f"vector index {idx['indexName']} in {name}",
                lambda n=idx['indexName']: s3v.delete_index(vectorBucketName=name, indexName=n))
        act(f"vector bucket {name}", lambda name=name: s3v.delete_vector_bucket(vectorBucketName=name))


# ─────────────────────────── 5. CloudFormation ───────────────────────────

def cleanup_stack():
    print("\n5. CloudFormation stack")
    cf = boto3.client('cloudformation', region_name=REGION)
    try:
        status = cf.describe_stacks(StackName=PREFIX)['Stacks'][0]['StackStatus']
    except ClientError:
        say(f"stack {PREFIX} does not exist")
        return
    say(f"stack {PREFIX} is {status}")

    def _delete():
        cf.delete_stack(StackName=PREFIX)
        wait_for_stack_gone(cf, PREFIX)
    act(f"stack {PREFIX} (tables, buckets, vector bucket + indexes, role, log group)", _delete)


# ─────────────────────────── 6. leftovers ───────────────────────────

def cleanup_leftovers(runtime_ids):
    print("\n6. Leftovers outside / after the stack")
    s3 = boto3.client('s3', region_name=REGION)
    for b in project_buckets(s3):
        act(f"empty + delete bucket {b}", lambda b=b: (empty_bucket(s3, b), s3.delete_bucket(Bucket=b)))
    cleanup_vector_buckets()

    ddb = boto3.client('dynamodb', region_name=REGION)
    for t in safe(lambda: ddb.list_tables()['TableNames'], []):
        if t.startswith(f"{PREFIX}-"):
            act(f"table {t}", lambda t=t: ddb.delete_table(TableName=t))

    iam = boto3.client('iam')
    role = f"{PREFIX}-agentcore-role"
    if safe(lambda: iam.get_role(RoleName=role), None):
        for name in safe(lambda: iam.list_role_policies(RoleName=role)['PolicyNames'], []):
            act(f"inline policy {name} of {role}",
                lambda name=name: iam.delete_role_policy(RoleName=role, PolicyName=name))
        act(f"role {role}", lambda: iam.delete_role(RoleName=role))

    logs = boto3.client('logs', region_name=REGION)
    prefixes = [f"/aws/bedrock/agentcore/{PREFIX}"] + \
               [f"/aws/bedrock-agentcore/runtimes/{rid}" for rid in runtime_ids]
    for p in prefixes:
        for g in safe(lambda: logs.describe_log_groups(logGroupNamePrefix=p)['logGroups'], []):
            act(f"log group {g['logGroupName']}",
                lambda n=g['logGroupName']: logs.delete_log_group(logGroupName=n))


# ─────────────────────────── 7. Transaction Search (optional) ───────────────────────────

def disable_transaction_search():
    print("\n7. CloudWatch Transaction Search")
    xray = boto3.client('xray', region_name=REGION)
    logs = boto3.client('logs', region_name=REGION)
    dest = safe(lambda: xray.get_trace_segment_destination(), {})
    if dest.get('Destination') == 'CloudWatchLogs':
        act("trace segment destination -> XRay (Transaction Search off)",
            lambda: xray.update_trace_segment_destination(Destination='XRay'))
    else:
        say("Transaction Search already off")
    for pol in safe(lambda: logs.describe_resource_policies()['resourcePolicies'], []):
        if pol['policyName'] == TS_POLICY_NAME:
            act(f"CloudWatch Logs resource policy {TS_POLICY_NAME}",
                lambda: logs.delete_resource_policy(policyName=TS_POLICY_NAME))
    for g in ('aws/spans', '/aws/application-signals/data'):
        for lg in safe(lambda: logs.describe_log_groups(logGroupNamePrefix=g)['logGroups'], []):
            if lg['logGroupName'] == g:
                act(f"log group {g}", lambda g=g: logs.delete_log_group(logGroupName=g))


# ─────────────────────────── main ───────────────────────────

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="Delete every AWS resource the NovaMart project created.")
    ap.add_argument('--yes', action='store_true', help='actually delete (default: dry run)')
    ap.add_argument('--disable-transaction-search', action='store_true',
                    help='also switch CloudWatch Transaction Search off and remove its resource policy')
    args = ap.parse_args()
    DRY = not args.yes

    ident = boto3.client('sts', region_name=REGION).get_caller_identity()
    print(f"{'DRY RUN - ' if DRY else ''}Cleaning up project '{PREFIX}' in {REGION} "
          f"as {ident['Arn']} (account {ident['Account']})")

    cleanup_knowledge_bases()
    runtime_ids = cleanup_agentcore()
    cleanup_guardrail()
    cleanup_bucket_contents()
    cleanup_stack()
    cleanup_leftovers(runtime_ids)
    if args.disable_transaction_search:
        disable_transaction_search()

    print()
    if DRY:
        print(f"{FOUND} deletion(s) planned. Re-run with --yes to execute.")
    else:
        print("Cleanup finished. Re-run once more to confirm nothing is left "
              "(AgentCore/KB deletions are asynchronous). Then clear the IDs in .env.")
