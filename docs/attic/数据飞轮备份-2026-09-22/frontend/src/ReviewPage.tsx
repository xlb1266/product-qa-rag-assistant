import { useEffect, useState } from 'react'
import { QaLibraryItem, ReviewItem, ReviewStatus, reviewsApi } from '../api'
import './ReviewPage.css'

interface ReviewPageProps {
  onOpenLog: (id: number) => void
}

const STATUS_TABS: Array<{ key: ReviewStatus | 'all'; label: string }> = [
  { key: 'pending', label: '待审核' },
  { key: 'approved', label: '已通过' },
  { key: 'revised', label: '修改后通过' },
  { key: 'rejected', label: '不通过' },
  { key: 'all', label: '全部' },
]

/** 质量标准（写死在页面上，审核人照着判，不凭感觉） */
const QUALITY_STANDARD = [
  '回答与问题直接相关，内容与知识库事实一致，无编造',
  '用户点赞（真实采纳信号），且不是「暂时不能回答」类兜底回复',
  '优先收录此前知识库没有覆盖或容易答错的场景',
  '已自动脱敏：不含手机号/身份证/银行卡号/邮箱等个人信息',
]

/** 数据审核页（仅管理员）：待审核问答三合一展示 + 三档审核 + 高质量库 */
export default function ReviewPage({ onOpenLog }: ReviewPageProps) {
  const [status, setStatus] = useState<ReviewStatus | 'all'>('pending')
  const [reviews, setReviews] = useState<ReviewItem[]>([])
  const [library, setLibrary] = useState<QaLibraryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  // 展开修订表单的记录 id
  const [revisingId, setRevisingId] = useState<number | null>(null)
  const [revQuestion, setRevQuestion] = useState('')
  const [revAnswer, setRevAnswer] = useState('')
  const [decidingId, setDecidingId] = useState<number | null>(null)

  const reload = (s: ReviewStatus | 'all') => {
    setLoading(true)
    Promise.all([reviewsApi.list(s), reviewsApi.qaLibrary()])
      .then(([rv, lib]) => {
        setReviews(rv)
        setLibrary(lib)
      })
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    reload(status)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  const decide = async (
    id: number,
    decision: ReviewStatus,
    extra?: { note?: string; revised_question?: string; revised_answer?: string }
  ) => {
    if (decidingId) return
    setDecidingId(id)
    setError('')
    try {
      const res = await reviewsApi.decide(id, decision, extra)
      setNotice(res.message + (res.document_id ? '（文档 #' + res.document_id + '）' : ''))
      setRevisingId(null)
      reload(status)
    } catch (e) {
      setError(e instanceof Error ? e.message : '审核操作失败')
    } finally {
      setDecidingId(null)
    }
  }

  const openRevise = (r: ReviewItem) => {
    setRevisingId(revisingId === r.id ? null : r.id)
    setRevQuestion(r.question)
    setRevAnswer(r.answer)
  }

  return (
    <div className="review-page">
      <h2>数据审核 · 飞轮数据反哺</h2>

      {notice && (
        <div className="review-page__notice">
          {notice}
          <button className="review-page__notice-close" onClick={() => setNotice('')}>×</button>
        </div>
      )}
      {error && <div className="review-page__error">{error}</div>}

      <div className="review-standard">
        <div className="review-standard__title">质量标准（照此判断，不凭感觉）</div>
        <ol className="review-standard__list">
          {QUALITY_STANDARD.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
        <div className="review-standard__privacy">
          数据用途与保存期限：仅用于改进问答质量（沉淀进知识库提升检索命中率），
          入池前已自动脱敏，不做任何对外共享；不通过的数据只保留审核标记。
        </div>
      </div>

      <div className="review-tabs">
        {STATUS_TABS.map((t) => (
          <button
            key={t.key}
            className={'review-tabs__btn' + (status === t.key ? ' review-tabs__btn--active' : '')}
            onClick={() => setStatus(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="review-page__empty">正在加载…</div>
      ) : reviews.length === 0 ? (
        <div className="review-page__empty">该状态下暂无数据</div>
      ) : (
        reviews.map((r) => (
          <div key={r.id} className="review-card">
            <div className="review-card__head">
              <span className="review-card__id">#{r.id}</span>
              <span className="review-card__user">贡献者 {r.user_email}</span>
              <span className="review-card__feedback">👍 用户点赞</span>
              {r.sanitized_types.length > 0 && (
                <span className="review-card__sanitized" title="入库前已自动脱敏">
                  已脱敏：{r.sanitized_types.join('、')}
                </span>
              )}
              {r.log_id != null && (
                <button className="review-card__loglink" onClick={() => onOpenLog(r.log_id!)}>
                  链路日志 #{r.log_id}
                </button>
              )}
              <span className="review-card__time">{r.created_at}</span>
            </div>
            <div className="review-card__body">
              <div className="review-card__field">
                <div className="review-card__label">用户问题（脱敏后）</div>
                <div className="review-card__text">{r.question}</div>
              </div>
              <div className="review-card__field">
                <div className="review-card__label">系统回答</div>
                <div className="review-card__text">{r.answer}</div>
              </div>
              {r.status === 'pending' ? (
                <>
                  <div className="review-card__actions">
                    <button
                      className="review-btn review-btn--approve"
                      disabled={decidingId === r.id}
                      onClick={() => decide(r.id, 'approved')}
                    >
                      通过（沉淀进知识库）
                    </button>
                    <button
                      className="review-btn review-btn--reject"
                      disabled={decidingId === r.id}
                      onClick={() => {
                        const note = window.prompt('不通过原因（可选，用于回查）：') ?? ''
                        decide(r.id, 'rejected', { note })
                      }}
                    >
                      不通过
                    </button>
                    <button
                      className="review-btn"
                      disabled={decidingId === r.id}
                      onClick={() => openRevise(r)}
                    >
                      修改后通过
                    </button>
                  </div>
                  {revisingId === r.id && (
                    <div className="review-revise">
                      <div className="review-card__label">修订后的问题</div>
                      <textarea
                        className="review-revise__input"
                        value={revQuestion}
                        onChange={(e) => setRevQuestion(e.target.value)}
                      />
                      <div className="review-card__label">修订后的回答</div>
                      <textarea
                        className="review-revise__input"
                        value={revAnswer}
                        onChange={(e) => setRevAnswer(e.target.value)}
                      />
                      <div className="review-card__actions">
                        <button
                          className="review-btn review-btn--approve"
                          disabled={decidingId === r.id || !revQuestion.trim() || !revAnswer.trim()}
                          onClick={() =>
                            decide(r.id, 'revised', {
                              revised_question: revQuestion,
                              revised_answer: revAnswer,
                            })
                          }
                        >
                          确认修订并入库（修订版会再次脱敏）
                        </button>
                        <button className="review-btn" onClick={() => setRevisingId(null)}>
                          取消
                        </button>
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <div className="review-card__result">
                  结论：
                  <span className={'review-status review-status--' + r.status}>
                    {r.status === 'approved' ? '通过' : r.status === 'revised' ? '修改后通过' : '不通过'}
                  </span>
                  {r.reviewer_email && <span className="review-card__soft"> 审核人 {r.reviewer_email}</span>}
                  {r.review_note && <span className="review-card__soft"> 备注：{r.review_note}</span>}
                </div>
              )}
            </div>
          </div>
        ))
      )}

      <h2>高质量问答库（已反哺知识库）</h2>
      {library.length === 0 ? (
        <div className="review-page__empty">暂无沉淀数据</div>
      ) : (
        <table className="review-table">
          <thead>
            <tr>
              <th>#</th>
              <th>问题</th>
              <th>回答</th>
              <th>来源</th>
              <th>知识库文档</th>
              <th>入库时间</th>
            </tr>
          </thead>
          <tbody>
            {library.map((q) => (
              <tr key={q.id}>
                <td className="review-table__soft">{q.id}</td>
                <td className="review-table__q">{q.question}</td>
                <td className="review-table__q">{q.answer}</td>
                <td className="review-table__soft">{q.source}</td>
                <td className="review-table__soft">#{q.document_id}</td>
                <td className="review-table__soft">{q.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
