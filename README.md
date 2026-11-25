# cnu-academic-helper

Cloud Architect 2602 assignment — 충남대학교 학사 공지·장학금 안내·졸업 요건 문서를 근거로 답변하고 출처를 함께 보여주는 RAG 챗봇 **CNU 학사 도우미**.

## 아키텍처

```
브라우저 ─▶ CloudFront ─┬─ /            ─▶ S3 SiteBucket (OAC, 퍼블릭 차단)
                        └─ /chat /feedback /health ─▶ API Gateway (HTTP API) ─▶ Lambda
                                                                       │
     ChatFunction: Bedrock KB Retrieve (상위 5개, 분야 필터) ─▶ 점수 0.5 미만 제외
                   ─▶ Claude(Converse)로 근거 기반 답변 ─▶ DynamoDB 대화 로그
     FeedbackFunction: 도움됨/안 됨 + 의견 ─▶ DynamoDB
     SyncFunction: DocsBucket 업로드·삭제 이벤트 ─▶ KB 재색인(StartIngestionJob)

DocsBucket(S3, 버전 관리) ─▶ Bedrock Knowledge Base (Titan Embeddings V2, OpenSearch Serverless)
```

화면과 API를 CloudFront 한 도메인으로 제공하므로 CORS 설정이 필요 없습니다.

## 구조

```
.
├── template.yaml          # AWS SAM 템플릿 (전체 인프라)
├── backend/
│   ├── health/app.py      # GET  /health
│   ├── chat/app.py        # POST /chat (handler), POST /feedback (feedback_handler)
│   ├── chat/test_app.py   # AWS 없이 도는 핸들러 체크
│   └── sync/app.py        # S3 이벤트 → KB 재색인
├── frontend/index.html    # 채팅 화면
└── docs/                  # 기획서, 아키텍처 문서
```

## 사전 준비

- AWS CLI 로그인, 리전 `ap-northeast-2`
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), Python 3.12
- Bedrock 콘솔 → **모델 액세스**에서 Titan Text Embeddings V2, Anthropic Claude 사용 허용

## 배포 (2단계)

Knowledge Base는 콘솔에서 만듭니다. 문서 버킷이 먼저 있어야 하므로 2번에 나눠 배포합니다.

**1차 — 인프라 생성**

```bash
sam build
sam deploy --guided        # 스택 이름 예: cnu-academic-helper, KnowledgeBaseId/DataSourceId는 비워둠
```

Outputs의 `DocsBucketName`, `SiteBucketName`, `DistributionId`, `SiteUrl`을 확인합니다.

**문서 업로드**

분야별 폴더에 원문(PDF, HTML; HWP는 PDF로 변환)과 같은 이름의 메타데이터 파일을 함께 올립니다.

```
학사/2026-학사운영시행세칙.pdf
학사/2026-학사운영시행세칙.pdf.metadata.json
```

```json
{
  "metadataAttributes": {
    "category": "학사",
    "title": "학사운영 시행세칙",
    "department": "학사지원과",
    "posted_date": "2026-03-01",
    "academic_year": 2026,
    "article": "제12조",
    "url": "https://..."
  }
}
```

- `category`는 `학사` / `장학` / `졸업` 중 하나여야 합니다(탭 필터에 사용).
- 가상 문서는 `title`에 `(가상 자료)`를 붙입니다.

```bash
aws s3 sync ./kb-docs s3://<DocsBucketName>/
```

**Knowledge Base 생성 (Bedrock 콘솔)**

1. 지식 기반 → 생성 → *벡터 저장소가 있는 지식 기반*
2. 데이터 소스: Amazon S3 → `s3://<DocsBucketName>/`
3. 청킹: 고정 크기, 최대 300토큰, 중첩 20%
4. 임베딩: Titan Text Embeddings V2, 1024차원
5. 벡터 저장소: *새 벡터 저장소 빠른 생성* → OpenSearch Serverless
6. 생성 후 **동기화** 실행, KB ID와 데이터 소스 ID를 복사

**2차 — KB 연결 + 화면 업로드**

```bash
sam deploy --parameter-overrides KnowledgeBaseId=<KB ID> DataSourceId=<데이터 소스 ID>
aws s3 sync frontend s3://<SiteBucketName>/ --delete
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*"
```

`SiteUrl`로 접속합니다. 이후 문서 업로드나 삭제는 SyncFunction이 자동으로 재색인합니다. 여러 파일을 한 번에 올린 경우에는 콘솔에서 동기화를 한 번 더 눌러 주세요.

답변 모델은 `ModelId` 파라미터로 바꿀 수 있습니다(기본값 `global.anthropic.claude-haiku-5-5`).

## 테스트

```bash
python backend/chat/test_app.py      # 핸들러 로직 (AWS 호출 없음)

curl $SITE_URL/health
curl -X POST $SITE_URL/chat -H "Content-Type: application/json" \
  -d '{"question": "졸업하려면 몇 학점이 필요해?", "category": "졸업"}'
```

## API

| Method | Path        | Body                                            | 응답 |
|--------|-------------|-------------------------------------------------|------|
| GET    | `/health`   | -                                               | `{"status": "ok"}` |
| POST   | `/chat`     | `{"question": "1~1000자", "category": "학사·장학·졸업 또는 빈 값", "user_id": "익명 ID"}` | `{"id", "answer", "insufficient", "sources": [{n, title, department, posted_date, article, page, excerpt, url, score}]}` |
| POST   | `/feedback` | `{"id": "<chat 응답 id>", "helpful": true, "comment": "선택"}` | `{"ok": true}` / 404 |

근거가 부족하면(검색 결과 없음, 유사도 0.5 미만, 또는 모델 판단) `insufficient: true`와 함께 안내 문구를 반환합니다.

## 로그

| 대상 | 위치 |
|------|------|
| API 접근 로그 (요청 ID, 상태, 지연 시간) | CloudWatch Logs `ApiAccessLogs` (30일) |
| Lambda 실행 로그 | CloudWatch Logs `/aws/lambda/<함수명>` |
| 질문·답변·출처·유사도·근거 부족 여부·피드백 | DynamoDB `ChatLogTable` (학번 대신 익명 ID) |

## 비용 주의

OpenSearch Serverless는 사용하지 않아도 최소 용량 요금이 계속 나갑니다. 과제가 끝나면 모두 삭제하세요.

```bash
aws s3 rm s3://<SiteBucketName> --recursive
sam delete
# Bedrock 콘솔에서 Knowledge Base 삭제 → OpenSearch Serverless 컬렉션 삭제
# DocsBucket은 버전 관리 중이라 sam delete 전에 콘솔에서 '비우기' 필요
```

## 아직 구현하지 않은 것 (기획서 대비)

- [ ] Cognito 로그인 + API Gateway JWT 인증, 관리자 그룹만 문서 업로드
- [ ] CloudWatch 오류율·지연 시간 경보
- [ ] Bedrock 모델 호출 로깅 (계정 단위 설정: Bedrock 콘솔 → 설정)
- [ ] CloudTrail 관리 이벤트 (계정에 기본 활성화된 이벤트 기록으로 대체)
- [ ] 학년도 메타데이터 필터, 최신 문서 우선 정렬
