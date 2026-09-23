import { useEffect, useState } from 'react'
import { LogListItem, logsApi } from '../api'
import './LogsPage.css'

interface LogsPageProps {
  onOpenLog: (id: number) => void
}

const INTENT_LABELS: Record<string, string> = {
  SUPPORT: '产品咨询',
  FEEDBACK: '问题反馈',
  CHAT: '闲聊',
  UNKNOWN: '未知',
}

export default function LogsPage({ onOpenLog }: LogsPageProps) {
  const [logs, setLogs] = useState<LogListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    logsApi
      .list(50)
      .then((items) => setLogs(items))
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <div className="logs-page"><div className="logs-page__empty">正在加载日志…</div></div>
  }
  if (error) {
    return <div className="logs-page"><div className="logs-page__empty">{error}</div></div>
  }

  return (
    <div className="logs-page">
      <h2>问答日志 · 最近 {logs.length} 条</h2>
      {logs.length === 0 ? (
        <div className="logs-page__empty">暂无日志记录</div>
      ) : (
        <table className="logs-page__table">
          <thead>
            <tr>
              <th>编号</th>
              <th>问题</th>
              <th>意图</th>
              <th>总耗时</th>
              <th>状态</th>
              {logs.some((l) => l.user_email) && <th>提问用户</th>}
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => (
              <tr
                key={log.id}
                className="logs-page__row"
                onClick={() => onOpenLog(log.id)}
              >
                <td className="logs-page__soft">#{log.id}</td>
                <td className="logs-page__question">{log.question}</td>
                <td>
                  <span className={'intent-tag intent-tag--' + log.intent_name.toLowerCase()}>
                    {INTENT_LABELS[log.intent_name] ?? log.intent_name}
                  </span>
                </td>
                <td className="logs-page__soft">{log.total_ms} ms</td>
                <td>
                  <span className={'status-tag status-tag--' + log.status}>
                    {log.status === 'success' ? '成功' : '失败'}
                  </span>
                </td>
                {logs.some((l) => l.user_email) && (
                  <td className="logs-page__soft">{log.user_email ?? '—'}</td>
                )}
                <td className="logs-page__soft">{log.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
