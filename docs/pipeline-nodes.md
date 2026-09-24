# 问答链路节点定义（全局统一）

> 本文档是日志（阶段 5）、调试（阶段 6）共用的节点与请求编号定义（数据飞轮已于 2026-09-22 移除）。
> 三个模块共用 logs 表的 `id` 作为**请求编号**，trace 数组中的节点按下表命名，不允许各搞一套。

## 请求编号

- 请求编号 = `logs.id`（SQLite 自增）
- 一条 logs 记录 = 一次完整问答请求（含失败）
- 调试页反查/重放、日志页详情，全部用这个编号串联

## 节点定义

| 节点名 | service | 说明 | 计时 |
|--------|---------|------|------|
| `intent_recognition` | DeepSeek | 意图识别 + 指代消解，输出 intent/confidence/resolved_question/reason | 编排层独立计时 |
| `retrieval` | 百炼embedding + FAISS + 本地BM25 | 语义 + BM25 双路召回、RRF 融合，query 向量化耗时报错都记在这 | 编排层独立计时（含重排） |
| `rerank` | 百炼 qwen3-rerank | 重排在 retrieve() 内部执行，编排层无法单独拆时 | duration_ms 恒为 0，耗时含入 retrieval 节点（output.note 注明） |
| `answerability` | 本地规则（无外部调用） | 代码层拒答预判：检索结果为空、或最高 rerank 分数低于 `retrieval.answerability_threshold` 时直接拒答，不进 LLM。output.answerable=false 即本次请求被拦截 | 本地判断，独立计时（通常 0ms） |
| `answer_generation` | DeepSeek | 基于检索知识生成回答，输出 useful/content | 编排层独立计时 |
| `chat_reply` | DeepSeek | CHAT 分支的闲聊回复 | 编排层独立计时 |

## 分支链路

- SUPPORT / FEEDBACK（知识库问答）：`intent_recognition → retrieval → rerank → answerability → answer_generation`
  - answerability 拦截时链路在此结束：trace 无 answer_generation 节点，消息与日志照常落库，回复用全系统兜底话术"暂时不能回答这个问题"
- CHAT（闲聊）：`intent_recognition → chat_reply`

## 节点字段

每个节点是如下结构的 JSON（存在 logs.trace，JSON 数组）：

```json
{
  "name": "retrieval",
  "service": "百炼embedding + FAISS + 本地BM25",
  "input": { "query": "指代消解后的问题" },
  "output": { "...": "节点输出摘要" },
  "duration_ms": 812,
  "status": "success | error | degraded",
  "error": "仅 status=error 时出现，含节点名 + 中文原因"
}
```

- `status=degraded`：重排失败/mock 跳过，降级用 RRF 排序，链路仍可用
- `output.mocked=true`：调试 mock 模式（参数 `debug.mock_third_party` 开启）下该节点未真实调用第三方，输入/输出为模拟值
- 错误定位：找 trace 中第一个 `status=error` 的节点，其 `error` 字段已包含"哪个节点、什么原因"；原始异常细节只在节点 error 文本里，不直接暴露给普通用户

## 可调参数（阶段 6，运行时热生效）

| 参数 key | 默认 | 范围 | 影响节点 |
|----------|------|------|----------|
| `retrieval.semantic_top_n` | 10 | 1-20 | retrieval（每路召回条数） |
| `retrieval.candidate_top_n` | 8 | 1-20 | retrieval（送重排候选数） |
| `retrieval.rerank_top_k` | 4 | 1-10 | rerank（最终保留条数） |
| `retrieval.rerank_threshold` | 0.6 | 0-1 | rerank（相关性过滤阈值） |
| `retrieval.answerability_threshold` | 0.6 | 0-1 | answerability（拒答预判阈值：最高 rerank 分数低于此值直接拒答；重排降级/mock 无真实分数时跳过分数判断） |
| `chat.max_history` | 20 | 1-50 | intent_recognition / answer_generation（上下文条数） |
| `debug.mock_third_party` | false | bool | 全部第三方调用点（mock 模拟返回） |

不开放页面修改的安全参数：密码加密规则（pbkdf2 迭代次数）、`auth_secret`、API Key（调试页只读打码展示）。
