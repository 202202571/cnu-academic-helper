import os

import boto3
from botocore.exceptions import ClientError

agent = boto3.client("bedrock-agent")


def handler(event, context):
    kb, ds = os.environ.get("KB_ID"), os.environ.get("DS_ID")
    if not kb or not ds:
        print("KB_ID/DS_ID 미설정: 재색인 건너뜀")
        return
    try:
        job = agent.start_ingestion_job(knowledgeBaseId=kb, dataSourceId=ds)
        print("ingestion started:", job["ingestionJob"]["ingestionJobId"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        # ponytail: 이미 색인 중이면 건너뜀. 그 사이 올린 파일은 빠질 수 있으니 여러 파일을 올린 뒤엔 콘솔에서 '동기화'를 한 번 더 누를 것
        print("ingestion already running, skipped")
