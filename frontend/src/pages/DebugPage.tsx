import { useEffect, useState } from 'react'
import {
  ConfigChangeItem,
  DebugConfig,
  DebugParam,
  ReplayResult,
  debugApi,
} from '../api'
import './DebugPage.css'

interface DebugPageProps {
  onOpenLog: (id: number) => void
}

type ParamDraft = Record<string, string | boolean>

/** 调试中心（仅管理员）：运行时参数（热生效）+ 修改历史/回滚 + 请求反查/重放 */
export default function DebugPage({ onOpenLog }: DebugPageProps) {
  const [config, setConfig] = useState<DebugConfig | null>(null)
  const [draft, setDraft] = useState<ParamDraft>({})
  const [changes, setChanges] = useState<ConfigChangeItem[]>([])
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  // 请求反查 / 重放
  const [logIdInput, setLogIdInput] = useState('')
  const [replaying, setReplaying] = useState(false)
  const [replayResult, setReplayResult] = useState<ReplayResult | null>(null)
  const [replayError, setReplayError] = useState('')

  const reload = () => {
    Promise.all([debugApi.getConfig(), debugApi.changes()])
      .then(([cfg, ch]) => {
        setConfig(cfg)
        setDraft(
          Object.fromEntries(
            cfg.params.map((p) => [p.key, p.type === 'bool' ? Boolean(p.current) : String(p.current)])
          ) as ParamDraft
        )
        setChanges(ch)
      })
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
  }

  useEffect(() => {
    reload()
    setLoading(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const saveAll = async () => {
    if (!config) return
    // 把草稿转成数值/布尔；本地先做一层格式检查，后端还会整体校验兜底
    const updates: Record<string, number | boolean> = {}
    for (const p of config.params) {
      const v = draft[p.key]
      if (p.type === 'bool') {
        updates[p.key] = Boolean(v)
      } else {
        const num = Number(v)
        if (!Number.isFinite(num)) {
          setError(p.label + '：请输入合法数字')
          return
        }
        updates[p.key] = num
      }
    }
    setError('')
    try {
      const res = await debugApi.updateConfig(updates)
      setNotice(res.message)
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    }
  }

  const doRollback = async (key: string) => {
    try {
      const res = await debugApi.rollback(key)
      setNotice(res.message + '（' + key + '）')
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : '回滚失败')
    }
  }

  const doReplay = async () => {
    const id = Number(logIdInput)
    if (!Number.isInteger(id) || id <= 0) {
      setReplayError('请输入有效的日志编号（正整数）')
      return
    }
    setReplayError('')
    setReplaying(true)
    setReplayResult(null)
    try {
      setReplayResult(await debugApi.replay(id))
    } catch (e) {
      setReplayError(e instanceof Error ? e.message : '重放失败')
    } finally {
      setReplaying(false)
    }
  }

  if (loading) {
    return <div className="debug-page"><div className="debug-page__empty">正在加载调试配置…</div></div>
  }
  if (!config) {
    return <div className="debug-page"><div className="debug-page__empty">{error || '加载失败'}</div></div>
  }

  const groups = Array.from(new Set(config.params.map((p) => p.group)))
  // changes 按 id DESC 返回（最新在前），但 new Map 保留最后一次出现的值，
  // 所以反转后再构造 Map，让每个 key 保留 id 最大（最新）的记录
  const lastChangeByKey = new Map([...changes].reverse().map((c) => [c.key, c]))

  return (
    <div className="debug-page">
      <h2>调试中心</h2>

      {notice && (
        <div className="debug-page__notice">
          {notice}
          <button className="debug-page__notice-close" onClick={() => setNotice('')}>×</button>
        </div>
      )}
      {error && <div className="debug-page__error">{error}</div>}

      <section className="debug-card">
        <div className="debug-card__head">
          <h3>运行时参数</h3>
          <p className="debug-card__hint">保存后立即生效（无需重启）；保存前整体校验，不合法的值会整条拒绝。</p>
        </div>
        {groups.map((g) => (
          <div key={g} className="debug-param-group">
            <div className="debug-param-group__title">{g}</div>
            {config.params
              .filter((p: DebugParam) => p.group === g)
              .map((p) => (
                <div key={p.key} className="debug-param">
                  <div className="debug-param__info">
                    <div className="debug-param__label">{p.label}</div>
                    <div className="debug-param__key">
                      {p.key} · 当前值 {String(p.current)} · 默认 {String(p.default)}
                      {p.type !== 'bool' && p.min !== null && ` · 范围 [${p.min}, ${p.max}]`}
                    </div>
                  </div>
                  {p.type === 'bool' ? (
                    <label className="debug-param__switch">
                      <input
                        type="checkbox"
                        checked={Boolean(draft[p.key])}
                        onChange={(e) => setDraft({ ...draft, [p.key]: e.target.checked })}
                      />
                      <span>{draft[p.key] ? '开启' : '关闭'}</span>
                    </label>
                  ) : (
                    <input
                      className="debug-param__input"
                      type="number"
                      step={p.type === 'float' ? '0.05' : '1'}
                      value={String(draft[p.key] ?? '')}
                      onChange={(e) => setDraft({ ...draft, [p.key]: e.target.value })}
                    />
                  )}
                </div>
              ))}
          </div>
        ))}
        <div className="debug-card__actions">
          <button className="debug-btn debug-btn--primary" onClick={saveAll}>
            保存全部参数（热生效）
          </button>
        </div>
      </section>

      <section className="debug-card">
        <div className="debug-card__head">
          <h3>请求反查与重放</h3>
          <p className="debug-card__hint">
            输入日志编号即可查看该次请求的完整链路，或把当时的输入重新跑一遍。
            开启「Mock 第三方调用」后重放不产生 API 费用。重放结果不写入消息和日志。
          </p>
        </div>
        <div className="debug-replay__bar">
          <input
            className="debug-replay__input"
            type="number"
            placeholder="日志编号（日志查看页第一列）"
            value={logIdInput}
            onChange={(e) => setLogIdInput(e.target.value)}
          />
          <button
            className="debug-btn"
            disabled={!logIdInput}
            onClick={() => onOpenLog(Number(logIdInput))}
          >
            查看链路日志
          </button>
          <button
            className="debug-btn debug-btn--primary"
            disabled={replaying || !logIdInput}
            onClick={doReplay}
          >
            {replaying ? '重放中…' : '重放该请求'}
          </button>
        </div>
        {replayError && <div className="debug-page__error">{replayError}</div>}
        {replayResult && (
          <div className="debug-replay__result">
            <div className="debug-replay__summary">
              <span>问题：{replayResult.question}</span>
              <span>
                状态：{replayResult.status} · 总耗时 {replayResult.total_ms}ms ·
                {replayResult.mock_used ? ' Mock 模式（未调第三方）' : ' 真实调用第三方'}
              </span>
              {replayResult.reply && <span>回答：{replayResult.reply}</span>}
              {replayResult.error && <span className="debug-replay__err">错误：{replayResult.error}</span>}
            </div>
            <div className="debug-replay__trace">
              {replayResult.trace.map((n, i) => (
                <div key={i} className="debug-replay__node">
                  <span className="debug-replay__idx">{i + 1}</span>
                  <span className="debug-replay__name">{n.name}</span>
                  <span className="debug-replay__service">{n.service}</span>
                  <span className={'debug-replay__status debug-replay__status--' + n.status}>
                    {n.status}
                  </span>
                  <span className="debug-replay__ms">{n.duration_ms}ms</span>
                  {n.output && 'mocked' in (n.output as Record<string, unknown>) && (
                    <span className="debug-replay__mock">mocked</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </section>

      <section className="debug-card">
        <div className="debug-card__head">
          <h3>参数修改历史</h3>
          <p className="debug-card__hint">每次变更记录操作人与前后值，可一键回滚到最近一次修改前的值。</p>
        </div>
        {changes.length === 0 ? (
          <div className="debug-page__empty">暂无修改记录</div>
        ) : (
          <table className="debug-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>参数</th>
                <th>变更</th>
                <th>操作人</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.id}>
                  <td className="debug-table__soft">{c.created_at}</td>
                  <td className="debug-table__mono">{c.key}</td>
                  <td>
                    {String(c.old_value)} <span className="debug-table__arrow">→</span> {String(c.new_value)}
                  </td>
                  <td>{c.updated_by}</td>
                  <td>
                    <button
                      className="debug-btn debug-btn--small"
                      disabled={lastChangeByKey.get(c.key)?.id !== c.id}
                      title={
                        lastChangeByKey.get(c.key)?.id !== c.id
                          ? '只能回滚该参数最近一次变更'
                          : '回滚到修改前的值'
                      }
                      onClick={() => doRollback(c.key)}
                    >
                      回滚
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="debug-card">
        <div className="debug-card__head">
          <h3>第三方服务信息（只读）</h3>
          <p className="debug-card__hint">密钥已打码，只展示部分尾号；修改密钥请在服务端 .env / 环境变量中配置。</p>
        </div>
        <table className="debug-table">
          <thead>
            <tr>
              <th>配置项</th>
              <th>值</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(config.services).map(([k, v]) => (
              <tr key={k}>
                <td className="debug-table__mono">{k}</td>
                <td className="debug-table__soft">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
