# 产品咨询智能客服 · 基于知识库的 AI 客服问答系统

一个使用 RAG（检索增强生成）技术的 AI 客服问答系统：用户提问后，系统先从知识库中检索相关内容，再交给大模型生成回答，回答更贴合业务资料、可溯源。

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python 3.12 + FastAPI + FAISS + SQLite |
| 依赖管理 | uv |
| 前端 | React 18 + TypeScript + Vite |
| 大模型 | DeepSeek API |
| 重排模型 | 阿里百炼 `qwen3-rerank` |
| 向量模型 | 阿里百炼 `text-embedding-v4` |

## 目录结构

```
kongqixiaozhu/
├── backend/          # 后端服务
│   ├── app/
│   │   ├── main.py          # FastAPI 入口
│   │   ├── config.py        # 配置（读取 .env）
│   │   ├── api/             # 路由（health / documents）
│   │   ├── core/            # chunker 分块 / embeddings 向量化 / vector_store FAISS
│   │   └── db/              # SQLite 访问层
│   └── data/                # SQLite 库与 FAISS 索引存放
├── frontend/         # 前端 React 应用
│   └── src/pages/           # 知识库管理 / 分块详情 / 在线咨询
├── test-data/        # 测试用知识库文档
└── README.md
```

## 功能：知识库文档管理（已实现）

上传 Markdown 文档后自动完成：**按标题层级分块 → text-embedding-v4 向量化（1024 维，每批 ≤10 段）→ FAISS 存向量 + SQLite 存文档与块信息**。

- 块的自增 id 即 FAISS 向量 ID，一一对应
- FAISS 使用 IndexIDMap2（归一化内积 = 余弦相似度），支持按 ID 删除，删文档时向量同步清理
- 前端「知识库管理」页支持上传、列表、删除、分块详情查看
- 向量化失败（Key 无效等）的文档会保留在列表中并标注失败原因，可删除后重传

接口一览：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/documents/upload` | 上传 .md 文件（multipart） |
| GET | `/api/documents` | 文档列表 |
| GET | `/api/documents/{id}` | 文档详情 + 分块列表 |
| DELETE | `/api/documents/{id}` | 删除文档（含向量） |
| GET | `/api/health` | 健康检查 |

## 快速开始

### 1. 后端

```bash
cd backend
cp .env.example .env      # 填入 API Key
uv sync                   # 安装依赖
uv run uvicorn app.main:app --reload --port 8000
```

启动后访问健康检查：http://127.0.0.1:8000/api/health

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

启动后访问：http://localhost:5173

> 当前阶段为项目骨架初始化，前后端均可独立启动，具体问答功能后续实现。
