# 横切能力说明（阶段 6/7/8，2026-08-23 完成）

三个横切模块的实现要点、接口清单与验收结果。节点与请求编号定义见 [pipeline-nodes.md](pipeline-nodes.md)。

## 阶段 7：权限管理

**角色**：`admin` / `user` 两类。role 存 users 表，`get_current_user` 每次从 DB 实时读取（token 不含角色，改角色后旧 token 权限立即生效）。

**三层边界**：

| 层 | 实现 |
|----|------|
| 页面显隐 | App.tsx 按 `user.role` 渲染 Tab：普通用户只见「在线咨询/日志查看」；管理员另见「知识库管理/用户管理/调试中心/数据审核」 |
| 接口校验 | `require_admin()` 依赖（security.py）：`/api/documents*`、`/api/users*`、`/api/debug*`、`/api/reviews*`、`/api/qa-library` 全部仅管理员；拦截时写 security_logs |
| 数据过滤 | messages/logs 按 `user_id` 过滤（不信前端传 ID）；管理员可看全部日志（带 user_email），普通用户 `scope=all` 越权尝试会被记录并按本人数据返回 |

**注册**：登录页自助注册（POST /api/auth/register），新用户一律 `user` 角色（最小权限）。

**角色变更**：PUT /api/users/{id}/role。要求携带管理员当前密码二次验证；不允许改自己的角色（防锁死）；每次变更写 role_changes（谁/何时/把谁/从什么改成什么）。用户管理页可视化操作。

**安全底线**：密码 pbkdf2_hmac 存储（随机盐、常量时间比较）；token 7 天过期（payload.exp，过期 401）；越权拦截全部写 security_logs（用户管理页可查）。

**验收**（全部实测通过）：双账号逐页边界、普通用户调管理接口 403+日志、篡改 log_id 查他人日志 403+日志、伪造过期 token 401、密码非明文、改角色密码错误被拒/正确成功+留痕、重复注册 400。

## 阶段 6：调试功能

**门禁**：全部调试接口仅管理员；页面参数中密钥只读打码展示（`sk-****尾4位`）。

**运行时参数**（app/core/runtime_config.py，定义/校验/留痕集中管理）：

| 参数 | 默认 | 范围 |
|------|------|------|
| retrieval.semantic_top_n | 10 | 1-20 |
| retrieval.candidate_top_n | 8 | 1-20 |
| retrieval.rerank_top_k | 4 | 1-10 |
| retrieval.rerank_threshold | 0.6 | 0-1 |
| chat.max_history | 20 | 1-50 |
| debug.mock_third_party | false | bool |

- **热生效**：业务代码（retrieval.py / chat.py）从内存单例实时读，保存即生效不重启；当前值持久化 runtime_config 表，重启恢复
- **整条校验**：set_many 先全部校验（类型/范围），任一不合法整条 400 拒绝，不出现"改了一半"
- **留痕+回滚**：每次变更写 config_changes（谁/何时/从多少到多少）；POST /api/debug/config/{key}/rollback 回滚到最近一次修改前（回滚本身也留痕）
- 不开放修改的安全参数：密码加密规则、auth_secret、API Key

**请求反查**：调试页输入日志编号 → 跳转该次请求的链路日志详情（节点/输入输出/耗时/报错在哪步），与日志页共用 logs.id 和节点定义。

**重放**：POST /api/debug/replay {log_id} 用当时的问题重跑链路，返回新 trace（不写消息不写日志）。`debug.mock_third_party=true` 时意图/回答/向量化/重排全部模拟返回（不花钱，节点标 mocked），BM25 本地索引照常执行，检索行为仍真实可见。

**验收**（实测）：改参数不重启生效、非法值（threshold=5）整条拒绝且其他参数未被带坏、mock 重放 4ms 完整链路并真实命中 BM25 检索、普通用户访问调试接口 403、回滚生效。

## 阶段 8：数据飞轮（已于 2026-09-22 整体移除，原件备份在 docs/attic/数据飞轮备份-2026-09-22/，以下为历史记录）

**闭环**：用户点赞 → 自动收集（脱敏后入 review_queue）→ 管理员审核（通过/不通过/修改后通过）→ 通过的 Q/A 生成 Markdown 走知识库入库管线（分块→向量化→FAISS）→ 下次相似问题检索命中；同时写 qa_library（与业务数据分开存，记来源/批次/对应文档 id）。

**收集规则**（api/feedback.py）：

- 只收集用户自己会话里 bot 消息的点赞；点踩只记录不进池
- 防护：兜底回复（useful=false）不收、闲聊回复不收、同一消息重复点赞幂等跳过
- 收集失败 try/except 不阻塞点赞主流程，失败信息进服务端日志
- 溯源：review_queue.log_id 反查该问答的链路日志（审核页可一键跳转）

**脱敏**（app/core/sanitizer.py）：入池前正则脱敏手机号/身份证/银行卡/邮箱为 `[已脱敏:类型]`，发现的类型随记录展示；管理员"修改后通过"提交的修订文本会再脱敏一次。

**审核页**（数据审核 Tab）：三合一展示（问题/回答/用户反馈）+ 固定质量标准（写死在页面上）+ 隐私用途说明 + 三档结论 + 高质量库列表。

**验收**（端到端实测）：含手机号的提问点赞后入池且 `13812345678 → [已脱敏:手机号]`；审核通过生成知识库文档（分块/向量化/ready）；换说法的相似问题（"能不能装在鸿蒙手机上用"）检索命中「高质量问答沉淀」文档——**反哺闭环成立**；普通用户访问审核接口 403、评价他人消息 403、重复审核 400、点踩/兜底回复不进池。

## 本次新增接口一览

| 方法 | 路径 | 权限 | 用途 |
|------|------|------|------|
| POST | /api/auth/register | 公开 | 自助注册（默认普通用户） |
| GET | /api/users | admin | 用户列表 |
| PUT | /api/users/{id}/role | admin | 改角色（密码二次验证+留痕） |
| GET | /api/users/role-changes | admin | 角色变更历史 |
| GET | /api/security-logs | admin | 安全事件（越权拦截） |
| GET | /api/debug/config | admin | 参数+打码服务信息 |
| PUT | /api/debug/config | admin | 批量改参数（热生效） |
| POST | /api/debug/config/{key}/rollback | admin | 回滚参数 |
| GET | /api/debug/config/changes | admin | 参数修改历史 |
| POST | /api/debug/replay | admin | 重放某次请求 |
| POST | /api/feedback | 登录 | 点赞/点踩（点赞自动收集） |
| GET | /api/reviews | admin | 审核池列表 |
| PUT | /api/reviews/{id} | admin | 审核结论（三档） |
| GET | /api/qa-library | admin | 高质量库列表 |

GET /api/logs 增强：管理员默认看全部（带 user_email），支持 scope=mine/all。
