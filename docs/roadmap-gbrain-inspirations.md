# Roadmap: GBrain から推察する HOKUSAI に入れるべき機能

**作成日**: 2026-05-27
**位置付け**: v0.5.1 dogfooding サイクル ([docs/dogfooding-findings.md](dogfooding-findings.md) §7-9, F1-F4 全解消) 完結後に書かれた **v0.6 以降の方向性議論メモ**。`garrytan/gbrain` (AI agent 用長期記憶エンジン) を調査し、HOKUSAI に取り込むべき / 取り込まない方がよい設計思想を整理した。コードに直結する実装計画ではなく、優先順位の合意形成のための土台ドキュメントとして扱う。

## 調査対象

- `garrytan/gbrain` master HEAD: `a74e5d90fca6e36dac1755fd53fc3e3a34a7394c` (`v0.41.20.0`)
- HOKUSAI local repository: `main`

## 結論

GBrainはHOKUSAIと同じ「開発ワークフローエンジン」ではない。中心価値は、AI agentのための長期記憶、検索、合成、知識グラフ、MCPツール面、常時メンテナンスにある。

HOKUSAIにそのまま取り込むべきではないが、以下の思想は強く参考になる。

1. Agentに渡す文脈は、単なる履歴一覧ではなく「引用つき合成 + 足りない情報の明示」にする。
2. CLI、MCP、Dashboard、将来の自動化から呼ぶ操作を、単一のoperation contractから生成する。
3. `doctor` / `status` は接続確認だけでなく、運用上の詰まりを一画面で判断できる健康診断にする。
4. 実ワークフローの検索・レビュー・修正結果を評価データとして保存し、変更後に replay できるようにする。
5. WorkgraphをNotion表示だけで終わらせず、ローカルにも探索可能な typed graph として扱う。

HOKUSAIでは「常駐パーソナルブレイン」を作るのではなく、「開発案件の記憶を次のworkflowに正しく渡す」方向に限定して取り込むのがよい。

## GBrainの特徴

調査で確認した主な特徴は以下。

- Markdown brain repoを正本にし、Postgres/PGLiteへ同期して検索する。
- `search` は raw retrieval、`think` は検索結果を合成し、引用とgap analysisを返す。
- Vector + BM25 + RRF + graph traversal + reranker を組み合わせる hybrid retrieval。
- `put_page` 時にwikilink/markdown link/frontmatter等からtyped edgeを抽出し、知識グラフを自動更新する。
- `operations.ts` に操作定義を集約し、CLI/MCP/HTTP/agent tool definitionsへ展開する contract-first 設計。
- MCPはstdio/HTTPを持ち、OAuth scope、source scope、remote/local trust boundary、rate limitを持つ。
- `doctor` はカテゴリ別スコアを持ち、`status` はsync/cycle/locks/workers/queue/autopilotを一画面で見る。
- Minions job queueにより、subagent、shell job、pause/resume、retry、token/cost、inbox、replayを扱う。
- `dream` / `autopilot` でリンク修復、要約、矛盾検出、embedding、orphansなどを定期実行する。
- eval capture / export / replay により、実検索クエリを退行テストの材料にできる。
- schema packsで、page type、link type、extractable/expert routingなどの知識構造を進化させる。
- 40以上のmarkdown skillをresolverでルーティングする「thin harness, fat skills」寄りの設計。

## HOKUSAIとの差分

HOKUSAIはすでに以下を持っている。

- 10フェーズのLangGraph workflow
- profile分離
- Notion Workflows / PR / Review Issues / Work Items / Workflow Gates / Project Memory連携
- `hokusai prime` によるactive Project Memory / Workgraph context出力
- SQLite store、LangGraph checkpoint、Notion outbox
- LLM Gatewayのprovider/model policy/audit基盤
- Operations Console、Slack通知、Figma/Miro連携

一方、GBrainに比べると次が弱い。

- `prime` はフィルタ済みNotion contextの整形であり、過去workflowからの検索・合成・gap analysisではない。
- CLI/Dashboard/将来MCPで共有するoperation contractがない。
- `doctor` / `status` が運用全体の詰まりを一画面で示す段階にはまだ弱い。
- 実workflowの結果を評価データとしてcapture/replayする仕組みがない。
- Workgraphの構造はNotion中心で、ローカルで横断探索・重複検出・再発検出するgraph primitiveは薄い。

## 取り込むべき機能

### P0. Prime v2: 引用つき合成とgap analysis

最優先。GBrainの最大の学びは「検索結果を並べるだけではなく、Agentが次に使える答えへ合成する」点にある。

HOKUSAIでは `hokusai prime` を次の方向へ拡張する。

- `workflow.db`、Notion Project Memory、Work Items、Review Issues、Gates、過去PR情報を検索対象にする。
- `hokusai prime <workflow-id> --query "..."`
- `hokusai recall "..."`
- `hokusai prime <workflow-id> --include-similar`
- 出力は「要点」「引用元」「未確定/不足情報」「今回のphaseで使うべき注意」に分ける。
- 引用元は `workflow_id`, phase, PR URL, Notion page ID, file path のように追跡可能にする。

初期実装でpgvectorは不要。まずSQLite FTS5 / BM25相当 + 決定的なフィルタで十分。高精度検索が必要になった段階で、GBrainを外部memory backendとして任意連携する方が安全。

### P0. Brain-firstならぬ Project-memory-first

GBrainは「外部APIやLLMに行く前にbrainを見る」ことを運用規律にしている。HOKUSAIではこれを「Phase実行前にProject Memory / 過去workflowを見る」に置き換える。

具体的には、Phase 2 / 4 / 5 / 8 のプロンプトに、必要最小限のprime contextを自動注入する。

- Phase 2: 過去の類似workflow、既知の設計判断、避けるべき実装
- Phase 4: 未解消のreview issue、既存Work Items、過去の失敗パターン
- Phase 5: active Project Memory、expected files、gate状態、同種修正の注意点
- Phase 8: 過去のCopilot/human reviewで再発した指摘

文脈注入は常に短くし、引用と上限tokenを持たせる。HOKUSAIのHuman-Orchestrated思想上、Agentが勝手にmemoryをactive化するのではなく、active memoryのみ注入対象にする方針は維持する。

### P1. Contract-first Operation Registry

GBrainの `operations.ts` は、操作名、説明、入力schema、handler、scopeを一箇所に集め、CLI/MCP/tools-jsonへ展開している。HOKUSAIも同じ方向に寄せる価値が高い。

候補となるHOKUSAI operation:

- `workflow.list`
- `workflow.status`
- `workflow.start`
- `workflow.continue`
- `workflow.cleanup`
- `prime.render`
- `workgraph.list_open_items`
- `review_issues.list_open`
- `gates.list_pending`
- `notion.outbox_status`
- `llm_gateway.audit_summary`
- `profile.doctor`

最初はMCPサーバーを作らなくてもよい。まずPython内にoperation registryを作り、CLIとDashboardが同じhandlerを呼ぶ構成にする。その後、read-only MCP / HTTP admin APIへ展開する。

### P1. Status / Doctorの一画面化

GBrainの `status` と `doctor` は、DB接続だけでなく、sync、cycle、queue、worker、autopilot、category scoreまで見せる。

HOKUSAIでは `hokusai status` / `hokusai profile doctor` / Operations Console を次の観点で統合する。

- profile config解決結果
- GitHub/GitLab CLI認証
- Notion DB ID env設定と実query可否
- Notion outbox pending/error件数
- Figma/Miro writeback outbox/error件数
- LLM Gateway audit永続化状態
- Worktree存在、branch、base branch freshness
- Review Issues open件数
- Workflow Gates pending/blocked件数
- Slack webhook疎通
- dashboard auth/port衝突

JSON出力はstable schemaにする。CIや運用監視が読みやすくなる。

### P1. Eval capture / replay

GBrainは実検索クエリをeval candidateとして保存し、export/replayできる。HOKUSAIでも、promptやworkflow品質の退行検知に同じ考え方が使える。

候補:

- Phase 2/3/4/7 の入出力とvalidation errorをcapture
- Phase 6 verification failureと修正後の結果をcapture
- Phase 8 review commentと修正結果をcapture
- `hokusai eval export --since 30d`
- `hokusai eval replay --fixture ...`
- `hokusai eval gate` でprompt変更やreview rule変更の退行を検出

保存時はprompt全文を避け、既存LLM Gatewayと同じくhash/length/metadataを基本にする。必要なfixtureだけ明示的にredacted本文を保存する。

### P1. Workgraphをローカルtyped graphとして扱う

HOKUSAIのWorkgraphはNotion上のhuman governance viewとして強い。一方で、ローカルにgraph queryできる形が薄い。

GBrainのtyped edge発想を借りて、HOKUSAI SQLiteに軽量なedge tableを追加する価値がある。

例:

- `workflow -> pull_request`
- `workflow -> work_item`
- `work_item -> blocked_by -> work_item`
- `review_issue -> resolved_by -> pull_request`
- `review_issue -> duplicates -> review_issue`
- `gate -> blocks -> work_item`
- `workflow -> supersedes -> workflow`
- `memory -> applies_to -> phase`
- `review_issue -> touches_file -> path`

抽出はLLMではなく、既存state、PR metadata、Phase 4 plan、review comment、Notion DB relationから決定的に行う。これにより、再発指摘、孤児PR、blockedの連鎖、handover chainをCLI/Operations Consoleで出せる。

### P2. General Job Queue

GBrainのMinionsは強力だが、HOKUSAIに同規模のagent queueを入れるのは重い。HOKUSAIではまず「background operational jobs」に限定する。

対象:

- Notion outbox再送
- Figma/Miro writeback再送
- stale workflow GC
- profile doctor定期実行
- LLM Gateway audit集計
- eval replay

LangGraph本体のphase制御は維持し、Minionsのような汎用subagent基盤へ寄せすぎない方がよい。

### P2. Profile Policy Packs

GBrainのschema packsは、知識の型をagentが進化させる仕組み。HOKUSAIではそのままではなく、profileごとの運用ポリシーpackとして取り入れるのが合う。

例:

- review severity taxonomy
- gate type set
- Project Memory type set
- allowed LLM model policy
- required verification commands
- design writeback policy

ただしNotion schema driftが起きやすいため、agentが自由に変更するのではなく、人間承認つきのmigrationとして扱う。

## 取り込まない方がよい機能

以下はHOKUSAIの目的から外れやすい。

- 個人の全生活ログを扱うpersonal/company brain本体
- email/calendar/voice/X/meeting transcriptの常時ingestion
- 40以上の汎用skillpackをHOKUSAIに同梱すること
- pgvector + reranker + multimodal retrievalを最初からHOKUSAI本体に持つこと
- OAuth付きmulti-user company brainをHOKUSAI単体で実装すること
- 常駐autopilot/dream cycleを開発workflow本体と同じ優先度で扱うこと

必要になればGBrainを外部memory backendとして連携すればよい。HOKUSAI本体は、workflow、governance、audit、human approvalを中核に置くべき。

## 推奨ロードマップ

### Step 1: Prime v2 MVP

- workflow artifactsをSQLiteにsearchable textとして保存
- `hokusai recall` を追加
- `hokusai prime --query` を追加
- 出力にcitations / gapsを入れる

詳細な設計議論は [docs/design-prime-v2.md](design-prime-v2.md) を参照。検索バックエンド選定 (SQLite FTS5 推奨)、引用データモデル、決定的な gap analysis 7 種、MVP 4 ステップ分割、未解決の設計問題 7 件を整理。

### Step 2: Doctor / Status統合

- outbox/error/gate/review issue/LLM Gateway auditをstatusに出す
- JSON schemaを固定
- Operations Consoleと共通handler化

### Step 3: Operation Registry

- read-only operationsからregistry化
- CLI handlerをregistry経由へ寄せる
- MCP/HTTP化はその後

### Step 4: Eval Capture

- phase入出力とreview loopのfixture export
- prompt変更時のreplay
- regression gateをCIまたはpre-releaseで実行

### Step 5: Local Workgraph Edges

- SQLite edge table
- deterministic extractor
- `hokusai graph query/status`
- recurring review issue検出

## 最終判断

GBrainからHOKUSAIが学ぶべきことは、RAGスタックそのものではなく「Agentが使う知識を、運用可能な形で蓄積・検索・合成・検証する仕組み」である。

HOKUSAIの次の強化点は、より多くの外部サービスを足すことではなく、既に蓄積しているworkflow履歴、review issue、gate、Project Memoryを、次のAgent実行に引用つきで戻すことである。
