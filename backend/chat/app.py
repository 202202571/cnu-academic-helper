import json
import os
import time
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

KB_ID = os.environ.get("KB_ID", "")
MODEL_ID = os.environ.get("MODEL_ID", "")
TOP_K = 5          # 기획서: 유사도 상위 5개 청크
MIN_SCORE = 0.5    # 기획서: 이 점수 미만이면 근거 부족
CATEGORIES = {"학사", "장학", "졸업"}  # 문서 메타데이터 category 값과 같아야 함
NO_ANSWER = "제공된 문서에서 근거를 찾을 수 없어 답변드리기 어렵습니다."
HELP = "학교 홈페이지(https://plus.cnu.ac.kr)의 학사·장학 공지 게시판이나 소속 학과 사무실에 문의해 주세요."
SYSTEM = """너는 충남대학교 학사·장학·졸업 안내 챗봇이다. <문서> 안의 내용만 근거로 한국어로 간결하게 답하라.
- 문서에 없는 내용은 추측하지 마라.
- 근거가 된 문단 번호를 문장 끝에 [1]처럼 표시하라.
- 문서만으로 답할 수 없으면 다른 말 없이 NO_ANSWER 만 출력하라."""

kb = boto3.client("bedrock-agent-runtime")
llm = boto3.client("bedrock-runtime")
table = boto3.resource("dynamodb").Table(os.environ.get("TABLE", "local"))


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _source(n, r):
    m = r.get("metadata", {})
    uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
    page = m.get("x-amz-bedrock-kb-document-page-number")
    return {
        "n": n,
        "title": m.get("title") or uri.rsplit("/", 1)[-1],
        "department": m.get("department", ""),
        "posted_date": m.get("posted_date", ""),
        "article": m.get("article", ""),
        "page": int(page) if page is not None else None,
        "excerpt": r["content"]["text"][:400],
        "url": m.get("url", ""),
        "score": round(r["score"], 3),
    }


def handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        question = (body.get("question") or "").strip()
        category = body.get("category") or ""
        user_id = str(body.get("user_id") or "anonymous")[:64]
    except (json.JSONDecodeError, AttributeError):
        return _resp(400, {"error": "body는 {question, category, user_id} JSON이어야 합니다"})
    if not question or len(question) > 1000:
        return _resp(400, {"error": "question은 1~1000자여야 합니다"})
    if category and category not in CATEGORIES:
        return _resp(400, {"error": f"category는 {sorted(CATEGORIES)} 중 하나이거나 비워야 합니다"})
    if not KB_ID:
        return _resp(503, {"error": "Knowledge Base가 아직 연결되지 않았습니다"})

    search = {"numberOfResults": TOP_K}
    if category:
        search["filter"] = {"equals": {"key": "category", "value": category}}
    results = kb.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": question},
        retrievalConfiguration={"vectorSearchConfiguration": search},
    )["retrievalResults"]
    hits = [r for r in results if r.get("score", 0) >= MIN_SCORE]
    sources = [_source(n, r) for n, r in enumerate(hits, 1)]

    answer = ""
    if hits:
        docs = "\n\n".join(f"[{s['n']}] {s['title']}\n{r['content']['text']}" for s, r in zip(sources, hits))
        out = llm.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": f"<문서>\n{docs}\n</문서>\n\n질문: {question}"}]}],
            inferenceConfig={"maxTokens": 800, "temperature": 0},
        )
        answer = out["output"]["message"]["content"][0]["text"].strip()
    insufficient = not hits or "NO_ANSWER" in answer
    if insufficient:
        answer, sources = f"{NO_ANSWER} {HELP}", []

    log_id = str(uuid.uuid4())
    try:
        table.put_item(Item={
            "id": log_id,
            "user_id": user_id,  # 학번 아님: 브라우저가 만든 익명 ID
            "ts": int(time.time()),
            "question": question,
            "category": category or "전체",
            "answer": answer,
            "insufficient": insufficient,
            "top_scores": [Decimal(str(round(r.get("score", 0), 3))) for r in results],
            "sources": [{"title": s["title"], "url": s["url"]} for s in sources],
        })
    except ClientError as e:  # 로그 실패로 답변까지 막지는 않음
        print("log failed:", e)
        log_id = None

    return _resp(200, {"id": log_id, "answer": answer, "insufficient": insufficient, "sources": sources})


def feedback_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        log_id, helpful = str(body["id"]), body["helpful"]
        comment = str(body.get("comment") or "")[:500]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return _resp(400, {"error": "body는 {id, helpful, comment} JSON이어야 합니다"})
    if not isinstance(helpful, bool):
        return _resp(400, {"error": "helpful은 true/false여야 합니다"})
    try:
        table.update_item(
            Key={"id": log_id},
            UpdateExpression="SET helpful = :h, #c = :c, feedback_ts = :t",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#c": "comment"},
            ExpressionAttributeValues={":h": helpful, ":c": comment, ":t": int(time.time())},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _resp(404, {"error": "해당 답변을 찾을 수 없습니다"})
        raise
    return _resp(200, {"ok": True})
