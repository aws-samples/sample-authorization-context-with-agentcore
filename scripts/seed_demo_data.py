"""
Seed demo data: uploads department-tagged documents to S3 and populates DynamoDB.

Usage:
    python scripts/seed_demo_data.py --bucket <bucket-name> --table <table-name>

Requires AWS credentials with s3:PutObject (+ tagging) and dynamodb:PutItem.
"""

import argparse
import json
import os
import sys

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')

SAMPLE_DOCS_DIR = os.path.join(os.path.dirname(__file__), '..', 'sample-docs')

# Map of department -> list of (local_path, s3_key)
DOCUMENTS = {
    'Finance': [
        ('finance/q3_2024_financial_report.txt', 'finance/reports/q3_2024.txt'),
        ('finance/q4_2024_budget_forecast.txt', 'finance/reports/q4_2024_budget.txt'),
        ('finance/vendor_payment_policy.txt', 'finance/policies/vendor_payment.txt'),
    ],
    'Sales': [
        ('sales/enterprise_playbook.txt', 'sales/playbooks/enterprise.txt'),
        ('sales/q4_pipeline_report.txt', 'sales/reports/q4_pipeline.txt'),
        ('sales/competitive_analysis_2024.txt', 'sales/reports/competitive_analysis.txt'),
    ],
}

# Sample DynamoDB records matching the blog's schema (PK=department, SK=entity)
DYNAMO_RECORDS = [
    # --- Finance records ---
    {
        'PK': 'Finance',
        'SK': 'CUSTOMER#123#INVOICE#2024-001',
        'amount': 45000,
        'status': 'Paid',
        'customer': 'Acme Corp',
        'due_date': '2024-09-15',
    },
    {
        'PK': 'Finance',
        'SK': 'CUSTOMER#124#INVOICE#2024-002',
        'amount': 12500,
        'status': 'Pending',
        'customer': 'Globex Inc',
        'due_date': '2024-10-01',
    },
    {
        'PK': 'Finance',
        'SK': 'CUSTOMER#125#INVOICE#2024-003',
        'amount': 78200,
        'status': 'Overdue',
        'customer': 'Initech',
        'due_date': '2024-08-30',
    },
    {
        'PK': 'Finance',
        'SK': 'CUSTOMER#126#INVOICE#2024-004',
        'amount': 320000,
        'status': 'Paid',
        'customer': 'Umbrella Corp',
        'due_date': '2024-07-15',
    },
    {
        'PK': 'Finance',
        'SK': 'CUSTOMER#127#INVOICE#2024-005',
        'amount': 18700,
        'status': 'WriteOff',
        'customer': 'Vandelay Industries',
        'due_date': '2024-05-01',
    },
    # --- Sales records ---
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#456#CONTRACT#ENT-2024',
        'value': 250000,
        'term_months': 24,
        'status': 'Active',
        'customer': 'Acme Corp',
        'start_date': '2024-01-01',
    },
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#457#CONTRACT#ENT-2023',
        'value': 180000,
        'term_months': 12,
        'status': 'Renewal',
        'customer': 'Globex Inc',
        'start_date': '2023-06-01',
    },
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#458#CONTRACT#ENT-2024-B',
        'value': 95000,
        'term_months': 36,
        'status': 'Negotiation',
        'customer': 'Initech',
        'start_date': '2024-11-01',
    },
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#459#OPPORTUNITY#OPP-2024-001',
        'value': 450000,
        'stage': 'Negotiation',
        'probability': 75,
        'customer': 'Acme Corp',
        'close_date': '2024-11-15',
    },
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#460#OPPORTUNITY#OPP-2024-002',
        'value': 750000,
        'stage': 'Discovery',
        'probability': 25,
        'customer': 'Umbrella Corp',
        'close_date': '2025-01-31',
    },
    {
        'PK': 'Sales',
        'SK': 'CUSTOMER#461#OPPORTUNITY#OPP-2024-003',
        'value': 280000,
        'stage': 'Demo',
        'probability': 40,
        'customer': 'Stark Industries',
        'close_date': '2024-12-15',
    },
]


def upload_documents(bucket: str) -> None:
    """Upload sample documents to S3 with department tags and KB metadata."""
    s3 = boto3.client('s3', region_name=AWS_REGION)

    for department, files in DOCUMENTS.items():
        for local_rel, s3_key in files:
            local_path = os.path.join(SAMPLE_DOCS_DIR, local_rel)
            if not os.path.exists(local_path):
                print(f"  SKIP  {local_rel} (file not found)")
                continue

            # Upload the document with S3 object tag
            with open(local_path, 'rb') as f:
                s3.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=f,
                    Tagging=f'Department={department}',
                )
            print(f"  OK    s3://{bucket}/{s3_key}  [Department={department}]")

            # Upload companion .metadata.json for Bedrock KB filtering
            metadata = {
                "metadataAttributes": {
                    "Department": department
                }
            }
            metadata_key = f"{s3_key}.metadata.json"
            s3.put_object(
                Bucket=bucket,
                Key=metadata_key,
                Body=json.dumps(metadata),
                ContentType='application/json',
            )
            print(f"  OK    s3://{bucket}/{metadata_key}  (KB metadata)")


def seed_dynamodb(table_name: str) -> None:
    """Write sample records to DynamoDB."""
    dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
    table = dynamodb.Table(table_name)

    with table.batch_writer() as batch:
        for item in DYNAMO_RECORDS:
            batch.put_item(Item=item)
            print(f"  OK    PK={item['PK']}  SK={item['SK']}")


def main():
    parser = argparse.ArgumentParser(description='Seed CustomerHub demo data')
    parser.add_argument('--bucket',
                        default=os.getenv('S3_BUCKET', ''),
                        help='S3 bucket name (default: S3_BUCKET from .env)')
    parser.add_argument('--table',
                        default=os.getenv('DYNAMODB_TABLE', ''),
                        help='DynamoDB table name (default: DYNAMODB_TABLE from .env)')
    args = parser.parse_args()

    if not args.bucket or not args.table:
        print("Error: --bucket and --table are required (or set S3_BUCKET and DYNAMODB_TABLE in .env).")
        sys.exit(1)

    print("\n--- Uploading documents to S3 ---")
    upload_documents(args.bucket)

    print("\n--- Seeding DynamoDB records ---")
    seed_dynamodb(args.table)

    print("\nDone. Demo data is ready.")


if __name__ == '__main__':
    main()
