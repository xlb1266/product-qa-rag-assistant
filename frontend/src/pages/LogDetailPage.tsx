import { useEffect, useState } from 'react'
import { LogDetail, LogTraceNode, logsApi } from '../api'
import './LogsPage.css'

interface LogDetailPageProps {
  logId: number
  onBack: () => void
}

const INTENT_LABELS: Record<string, string> = {
  SUPPORT: '产品咨询',
  FEEDBACK: '问题反馈',
  CHAT: '闲聊',
  UNKNOWN: '未知',
}

const NODE_LABELS: Record<string, string> = {
  intent_recognition: '意图识别',
  retrieval: '知识检索',
  rerank: '重排',
  answer_generation: '回答生成',
  chat_reply: '闲聊回复',
}

function formatJson(value: unknown): string {
  if (value === null || value === undefined) return '（无）'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function statusLabel(status: string): string {
  if (status === 'success') return '成功'
  if (status === 'error') return '失败'
  if (status === 'degraded') return '降级'
  return status
}

export default function LogDetailPage({ logId, onBack }: LogDetailPageProps) {
  const [detail, setDetail] = useState<LogDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    logsApi
      .get(logId)
      .then((d) => setDetail(d))
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false))
  }, [logId])

  if (loading) {
    return <div className="log-detail"><div className="logs-page__empty">正在加载日志详情…</div></div>
  }
  if (error || !detail) {
    return (
      <div className="log-detail">
        <button className="log-detail__back" onClick={onBack}>← 返回列表</button>
        <div className="logs-page__empty">{error || '日志不存在'}</div>
      </div>
    )
  }

  return (
    <div className="log-detail">
      <button className="log-detail__back" onClick={onBack}>← 返回列表</button>

      {/* 顶部摘要 */}
      <div className="log-detail__summary">
        <div className="log-detail__question">{detail.question}</div>
        <div className="log-detail__meta">
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">日志编号</span>
            <span className="log-detail__meta-value">#{detail.id}</span>
          </div>
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">意图</span>
            <span className="log-detail__meta-value">
              {INTENT_LABELS[detail.intent_name] ?? detail.intent_name}
            </span>
          </div>
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">总耗时</span>
            <span className="log-detail__meta-value">{detail.total_ms} ms</span>
          </div>
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">状态</span>
            <span className={'status-tag status-tag--' + detail.status}>
              {statusLabel(detail.status)}
            </span>
          </div>
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">节点数</span>
            <span className="log-detail__meta-value">{detail.trace.length}</span>
          </div>
          <div className="log-detail__meta-item">
            <span className="log-detail__meta-label">时间</span>
            <span className="log-detail__meta-value">{detail.created_at}</span>
          </div>
        </div>
      </div>

      {/* 全链路节点时间线 */}
      <div className="log-detail__trace">
        {detail.trace.map((node: LogTraceNode, i: number) => (
          <div key={i} className="trace-node">
            <div className="trace-node__head">
              <span className="trace-node__index">{i + 1}</span>
              <span className="trace-node__name">
                {NODE_LABELS[node.name] ?? node.name}
              </span>
              <span className="trace-node__service">{node.service}</span>
              <span className={'status-tag status-tag--' + node.status}>
                {statusLabel(node.status)}
              </span>
              <span className="trace-node__duration">{node.duration_ms} ms</span>
            </div>
            <div className="trace-node__body">
              {node.error && <div className="trace-node__error">{node.error}</div>}
              <div className="trace-field">
                <div className="trace-field__label">输入参数</div>
                <pre className="trace-field__content">{formatJson(node.input)}</pre>
              </div>
              <div className="trace-field">
                <div className="trace-field__label">响应结果</div>
                <pre className="trace-field__content">{formatJson(node.output)}</pre>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
