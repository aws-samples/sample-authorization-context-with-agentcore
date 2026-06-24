"""
Create Bedrock Knowledge Base with S3 Vectors storage and S3 data source.

This script:
1. Creates a Bedrock Knowledge Base using S3 Vectors as the vector store
2. Creates an S3 data source pointing to the knowledge base bucket
3. Starts an ingestion sync job
4. Outputs the Knowledge Base ID for .env

Usage:
    python scripts/create_knowledge_base.py --env dev

Requires:
    - CloudFormation stack deployed (for S3 bucket and IAM role)
    - Sample docs uploaded to S3 (run seed_demo_data.py first)
"""

import argparse
import json
import os
import sys
import time

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')


def get_stack_outputs(env: str) -> dict:
    """Get outputs from the CloudFormation stack."""
    cfn = boto3.client('cloudformation', region_name=AWS_REGION)
    stack_name = "crmhub"

    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        outputs = resp['Stacks'][0]['Outputs']
        return {o['OutputKey']: o['OutputValue'] for o in outputs}
    except Exception as e:
        print(f"Error reading stack '{stack_name}': {e}")
        print("Make sure the CloudFormation stack is deployed first.")
        sys.exit(1)


def create_knowledge_base(role_arn: str, env: str) -> dict:
    """Create a Bedrock Knowledge Base with S3 Vectors storage."""
    client = boto3.client('bedrock-agent', region_name=AWS_REGION)
    kb_name = f"CustomerHub-KB-{env}"

    # Check if KB already exists
    existing = client.list_knowledge_bases(maxResults=100)
    for kb in existing.get('knowledgeBaseSummaries', []):
        if kb['name'] == kb_name:
            print(f"Knowledge Base '{kb_name}' already exists: {kb['knowledgeBaseId']}")
            return {'knowledgeBaseId': kb['knowledgeBaseId'], 'existed': True}

    print(f"Creating Knowledge Base '{kb_name}'...")
    response = client.create_knowledge_base(
        name=kb_name,
        description=f"CustomerHub department-scoped knowledge base ({env})",
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            'type': 'VECTOR',
            'vectorKnowledgeBaseConfiguration': {
                'embeddingModelArn': f'arn:aws:bedrock:{AWS_REGION}::foundation-model/amazon.titan-embed-text-v2:0'
            }
        },
        storageConfiguration={
            'type': 'S3_VECTORS',
            's3VectorsConfiguration': {
                'bucketArn': f'arn:aws:s3vectors:{AWS_REGION}:{get_account_id()}:bucket/customerhub-kb-vectors-{env}'
            }
        },
        tags={
            'Project': 'CustomerHub',
            'Environment': env
        }
    )

    kb_id = response['knowledgeBase']['knowledgeBaseId']
    status = response['knowledgeBase']['status']
    print(f"Knowledge Base created: {kb_id} (status: {status})")

    # Wait for KB to become ACTIVE
    print("Waiting for Knowledge Base to become ACTIVE...")
    for _ in range(30):
        time.sleep(5)
        kb = client.get_knowledge_base(knowledgeBaseId=kb_id)
        status = kb['knowledgeBase']['status']
        if status == 'ACTIVE':
            print(f"Knowledge Base is ACTIVE.")
            break
        elif status == 'FAILED':
            reason = kb['knowledgeBase'].get('failureReasons', ['Unknown'])
            print(f"Knowledge Base creation FAILED: {reason}")
            sys.exit(1)
        print(f"  Status: {status}...")
    else:
        print("Timed out waiting for Knowledge Base to become ACTIVE.")
        sys.exit(1)

    return {'knowledgeBaseId': kb_id, 'existed': False}


def create_data_source(kb_id: str, bucket_arn: str, env: str) -> str:
    """Create an S3 data source for the Knowledge Base."""
    client = boto3.client('bedrock-agent', region_name=AWS_REGION)
    ds_name = f"customerhub-s3-source-{env}"

    # Check if data source already exists
    existing = client.list_data_sources(knowledgeBaseId=kb_id, maxResults=100)
    for ds in existing.get('dataSourceSummaries', []):
        if ds['name'] == ds_name:
            print(f"Data source '{ds_name}' already exists: {ds['dataSourceId']}")
            return ds['dataSourceId']

    print(f"Creating S3 data source '{ds_name}'...")
    response = client.create_data_source(
        knowledgeBaseId=kb_id,
        name=ds_name,
        description=f"S3 bucket with department-tagged documents ({env})",
        dataSourceConfiguration={
            'type': 'S3',
            's3Configuration': {
                'bucketArn': bucket_arn
            }
        }
    )

    ds_id = response['dataSource']['dataSourceId']
    print(f"Data source created: {ds_id}")
    return ds_id


def start_ingestion(kb_id: str, ds_id: str) -> None:
    """Start a sync/ingestion job."""
    client = boto3.client('bedrock-agent', region_name=AWS_REGION)

    print("Starting ingestion sync...")
    response = client.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id
    )

    job_id = response['ingestionJob']['ingestionJobId']
    status = response['ingestionJob']['status']
    print(f"Ingestion job started: {job_id} (status: {status})")

    # Poll until complete
    print("Waiting for ingestion to complete...")
    for _ in range(60):
        time.sleep(10)
        job = client.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=ds_id,
            ingestionJobId=job_id
        )
        status = job['ingestionJob']['status']
        stats = job['ingestionJob'].get('statistics', {})

        if status == 'COMPLETE':
            print(f"Ingestion COMPLETE.")
            print(f"  Documents scanned: {stats.get('numberOfDocumentsScanned', 'N/A')}")
            print(f"  Documents indexed: {stats.get('numberOfNewDocumentsIndexed', 0) + stats.get('numberOfModifiedDocumentsIndexed', 0)}")
            print(f"  Documents failed:  {stats.get('numberOfDocumentsFailed', 0)}")
            return
        elif status == 'FAILED':
            reasons = job['ingestionJob'].get('failureReasons', ['Unknown'])
            print(f"Ingestion FAILED: {reasons}")
            return
        print(f"  Status: {status}...")

    print("Timed out waiting for ingestion. Check the console for status.")


def get_account_id() -> str:
    sts = boto3.client('sts', region_name=AWS_REGION)
    return sts.get_caller_identity()['Account']


def main():
    parser = argparse.ArgumentParser(description='Create Bedrock Knowledge Base')
    parser.add_argument('--env', default='dev', choices=['dev', 'prod'])
    parser.add_argument('--skip-sync', action='store_true', help='Skip ingestion sync')
    args = parser.parse_args()

    # Get stack outputs
    outputs = get_stack_outputs(args.env)
    bucket_name = outputs.get('KnowledgeBaseBucketName', '')
    bucket_arn = outputs.get('KnowledgeBaseBucketArn', '')
    role_arn = outputs.get('KnowledgeBaseRoleArn', '')

    if not bucket_arn or not role_arn:
        print("Error: Missing stack outputs. Deploy CloudFormation first.")
        sys.exit(1)

    print(f"S3 Bucket: {bucket_name}")
    print(f"KB Role:   {role_arn}")
    print()

    # 1. Create Knowledge Base
    kb_result = create_knowledge_base(role_arn, args.env)
    kb_id = kb_result['knowledgeBaseId']

    # 2. Create S3 data source
    ds_id = create_data_source(kb_id, bucket_arn, args.env)

    # 3. Start ingestion
    if not args.skip_sync:
        start_ingestion(kb_id, ds_id)

    # Output
    print()
    print("=" * 60)
    print(f"Knowledge Base ID: {kb_id}")
    print("=" * 60)
    print(f"\nAdd this to your .env file:")
    print(f"KNOWLEDGE_BASE_ID={kb_id}")
    print(f"\nAlso set it as an env var on the AgentCore runtime.")


if __name__ == '__main__':
    main()
