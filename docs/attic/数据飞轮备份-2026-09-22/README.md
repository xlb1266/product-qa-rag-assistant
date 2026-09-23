# 数据飞轮备份（2026-09-22 移除）

数据飞轮功能（点赞/点踩收集 → 脱敏 → 人工审核 → 问答沉淀知识库）已于 2026-09-22 从项目中整体移除，决策原因：该功能不进入本轮四阶段迭代（评测/防幻觉/PDF+Word/chunking），且面试口径为"不主动提"。

本目录是移除前的完整原件备份（目录结构与项目内路径一一对应）：

**整文件删除的：**
- `backend/app/api/feedback.py` — 飞轮全部接口（/feedback、/reviews、/qa-library）
- `backend/app/core/sanitizer.py` — 入池前脱敏（仅飞轮使用）
- `frontend/src/pages/ReviewPage.tsx` / `ReviewPage.css` — 数据审核页

**改动前原版的（对应文件在项目里仍有，但内容已去飞轮化）：**
- `backend/app/main.py` — 去掉了 feedback_router 注册
- `backend/app/api/documents.py` — 仅改注释（去掉"与数据飞轮反哺共用"表述）
- `backend/app/api/messages.py` — SELECT 去掉 feedback / feedback_at 列
- `backend/app/db/database.py` — 建表去掉 review_queue、qa_library 两表及 messages 的 feedback 两列
- `frontend/src/api.ts` — 去掉 feedbackApi / reviewsApi / ReviewItem / QaLibraryItem
- `frontend/src/App.tsx` — 去掉 review 视图与导航入口
- `frontend/src/pages/ChatPage.tsx` / `ChatPage.css` — 去掉点赞/点踩按钮与样式
- `docs/requirements.md` / `cross-cutting.md` / `pipeline-nodes.md` — 加了移除标注

注意：`backend/data/knowledge.db` 旧库中已存在的 review_queue、qa_library 表和 messages 的 feedback 列**保留未删**（历史数据不动的原则），只是代码不再读写；新库不会再建这些表。如需恢复功能，把本目录文件拷回并反向应用上述改动即可。
