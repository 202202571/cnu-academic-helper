"""AWS 없이 도는 핸들러 체크: python backend/chat/test_app.py"""
import json
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ["KB_ID"] = "kb-test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402


class Fake:
    def __init__(self, **ret):
        self.ret, self.calls = ret, []

    def __getattr__(self, name):
        def call(**kw):
            self.calls.append((name, kw))
            r = self.ret[name]
            if isinstance(r, Exception):
                raise r
            return r
        return call


def hit(score, text="졸업 학점은 130학점 이상이다."):
    return {"score": score, "content": {"text": text},
            "location": {"s3Location": {"uri": "s3://b/졸업/rule.pdf"}},
            "metadata": {"title": "학사운영 시행세칙", "x-amz-bedrock-kb-document-page-number": 3.0,
                         "url": "https://example.ac.kr/rule"}}


def chat(body, results, answer="130학점 이상이 필요합니다 [1]"):
    app.kb = Fake(retrieve={"retrievalResults": results})
    app.llm = Fake(converse={"output": {"message": {"content": [{"text": answer}]}}})
    app.table = Fake(put_item={})
    r = app.handler({"body": json.dumps(body)}, None)
    return r["statusCode"], json.loads(r["body"])


# 정상 답변 + 분야 필터 + 기준 미만 청크 제외
code, out = chat({"question": "졸업 학점?", "category": "졸업"}, [hit(0.8), hit(0.3)])
assert code == 200 and not out["insufficient"] and out["id"], out
assert len(out["sources"]) == 1 and out["sources"][0]["page"] == 3
assert app.kb.calls[0][1]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["equals"]["value"] == "졸업"
assert app.table.calls[0][1]["Item"]["category"] == "졸업"

# 전체 탭이면 필터 없음
code, out = chat({"question": "졸업 학점?"}, [hit(0.8)])
assert "filter" not in app.kb.calls[0][1]["retrievalConfiguration"]["vectorSearchConfiguration"]

# 유사도 전부 기준 미만 → 모델 호출 없이 근거 부족
code, out = chat({"question": "학식 메뉴?"}, [hit(0.2)])
assert out["insufficient"] and out["sources"] == [] and app.llm.calls == []

# 모델이 근거 부족 판단
code, out = chat({"question": "학식 메뉴?"}, [hit(0.7)], answer="NO_ANSWER")
assert out["insufficient"] and app.NO_ANSWER in out["answer"]

# 입력 검증
assert chat({"question": " "}, [])[0] == 400
assert chat({"question": "q", "category": "기숙사"}, [])[0] == 400
assert app.handler({"body": "[1]"}, None)["statusCode"] == 400

# 피드백
app.table = Fake(update_item={})
fb = lambda b: app.feedback_handler({"body": json.dumps(b)}, None)["statusCode"]  # noqa: E731
assert fb({"id": "x", "helpful": True, "comment": "좋아요"}) == 200
assert fb({"id": "x", "helpful": "yes"}) == 400
assert fb({"helpful": True}) == 400
app.table = Fake(update_item=ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"))
assert fb({"id": "nope", "helpful": False}) == 404

print("ok")
