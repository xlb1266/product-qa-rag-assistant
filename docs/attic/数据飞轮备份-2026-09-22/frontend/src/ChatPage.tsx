import { useEffect, useRef, useState } from 'react'
import { AnswerInfo, ChatIntent, chatApi, feedbackApi, IntentName, MessageItem, messagesApi, RetrievalInfo } from '../api'
import './ChatPage.css'

/** 前端渲染用的消息：DB 消息 + 本地欢迎语 + 乐观临时消息 */
interface Message {
  /** DB 自增 id；本地欢迎语/乐观临时消息用负数避免与 DB id 冲突 */
  id: number
  role: 'user' | 'bot'
  content: string
  /** 本地占位消息（欢迎语），不进入后端历史 */
  local?: boolean
  /** 意图识别结果，仅 assistant 携带 */
  intent?: ChatIntent | null
  /** 知识库回答结果，仅 SUPPORT/FEEDBACK 回复携带 */
  answer?: AnswerInfo | null
  /** 知识库检索结果，仅 SUPPORT/FEEDBACK 回复携带 */
  retrieval?: RetrievalInfo | null
  /** 触发本轮回复的用户原话，用于对比改写前后的问题 */
  question?: string | null
  /** 1 点赞 / -1 点踩 / undefined 未评价 */
  feedback?: number | null
  created_at?: string
}

const INTENT_LABELS: Record<IntentName, string> = {
  SUPPORT: '产品咨询',
  FEEDBACK: '问题反馈',
  CHAT: '闲聊',
}

const WELCOME_MESSAGE: Message = {
  id: -1,
  role: 'bot',
  local: true,
  content: '你好，我是产品咨询智能客服，有什么可以帮您？',
}

/** 把后端 MessageItem 转成前端渲染 Message（assistant -> bot） */
function toMessage(m: MessageItem): Message {
  return {
    id: m.id,
    role: m.role === 'user' ? 'user' : 'bot',
    content: m.content,
    intent: m.intent,
    answer: m.answer,
    retrieval: m.retrieval,
    question: m.question,
    feedback: m.feedback,
    created_at: m.created_at,
  }
}

let _pendingSeq = -100 // 乐观临时消息的 id 生成器，负数避免与 DB id 冲突

export default function ChatPage() {
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [clearing, setClearing] = useState(false)
  const listRef = useRef<HTMLDivElement>(null)

  // 进入会话页：从后端拉取最近 50 条历史消息
  useEffect(() => {
    messagesApi
      .list(50)
      .then((items) => {
        const history = items.map(toMessage)
        // 历史为空时前面插一条欢迎语（本地占位，不进 DB、不进 LLM 历史）
        setMessages(history.length > 0 ? history : [WELCOME_MESSAGE])
      })
      .catch(() => {
        setMessages([WELCOME_MESSAGE])
      })
      .finally(() => setLoadingHistory(false))
  }, [])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages])

  const handleSend = async () => {
    const text = input.trim()
    if (!text || sending) return

    // 乐观显示用户消息（临时负 id，等后端返回真实 id 后替换）
    const pendingId = _pendingSeq--
    setMessages((prev) => [...prev, { id: pendingId, role: 'user', content: text }])
    setInput('')
    setSending(true)

    try {
      const res = await chatApi.send(text)
      // 用后端返回的真实记录替换临时 user 消息，并追加 bot 回复
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== pendingId),
        toMessage(res.user_message),
        toMessage(res.bot_message),
      ])
    } catch (e) {
      // 失败移除临时 user 消息并提示
      const msg = e instanceof Error ? e.message : '未知错误'
      setMessages((prev) => [
        ...prev.filter((m) => m.id !== pendingId),
        { id: _pendingSeq--, role: 'bot', local: true, content: '抱歉，服务暂时不可用（' + msg + '），请稍后重试。' },
      ])
    } finally {
      setSending(false)
    }
  }

  const handleFeedback = async (messageId: number, rating: 1 | -1) => {
    // 已是相同评价则取消本次点击（避免重复提交）；后端本身幂等覆盖
    setMessages((prev) =>
      prev.map((m) => (m.id === messageId ? { ...m, feedback: rating } : m))
    )
    try {
      await feedbackApi.submit(messageId, rating)
    } catch (e) {
      // 提交失败回滚本地状态
      const msg = e instanceof Error ? e.message : '未知错误'
      setMessages((prev) =>
        prev.map((m) => (m.id === messageId ? { ...m, feedback: undefined } : m))
      )
      window.alert('提交反馈失败：' + msg)
    }
  }

  const handleClear = async () => {
    if (clearing || sending) return
    // 防误删：清空不可恢复，先确认
    if (!window.confirm('确定清空所有聊天记录吗？此操作不可恢复。')) return
    setClearing(true)
    try {
      await messagesApi.clear()
      // 回到初始欢迎语，与首次进入一致
      setMessages([WELCOME_MESSAGE])
    } catch (e) {
      const msg = e instanceof Error ? e.message : '未知错误'
      window.alert('清空失败：' + msg)
    } finally {
      setClearing(false)
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-page__list" ref={listRef}>
        {loadingHistory && (
          <div className="bubble bubble--bot bubble--typing">正在加载历史消息…</div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={'bubble bubble--' + m.role}>
            <div className="bubble__content">{m.content}</div>
            {m.intent && (
              <div className="bubble__meta">
                <span className={'intent-tag intent-tag--' + m.intent.name.toLowerCase()}>
                  {INTENT_LABELS[m.intent.name] ?? m.intent.name}
                </span>
                {m.answer && (
                  <span className={'useful-tag ' + (m.answer.useful ? 'useful-tag--yes' : 'useful-tag--no')}>
                    {m.answer.useful ? '已基于知识库回答' : '知识库无法回答'}
                  </span>
                )}
                <span className="bubble__confidence">
                  置信度 {Math.round(m.intent.confidence * 100)}%
                </span>
                {m.question && m.intent.resolved_question !== m.question && (
                  <span className="bubble__rewritten">改写后：{m.intent.resolved_question}</span>
                )}
              </div>
            )}
            {m.retrieval && m.retrieval.chunks.length > 0 && (
              <details className="retrieval-panel">
                <summary className="retrieval-panel__summary">
                  知识库参考 · {m.retrieval.chunks.length} 条
                  {!m.retrieval.rerank_used && '（重排降级）'}
                </summary>
                {m.retrieval.chunks.map((c, i) => (
                  <div key={c.chunk_id} className="retrieval-item">
                    <div className="retrieval-item__head">
                      <span className="retrieval-item__title">
                        {i + 1}. {c.heading_path || c.document_title}
                      </span>
                      <span className="retrieval-item__score">
                        相关度 {(c.score * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="retrieval-item__content">{c.content}</div>
                  </div>
                ))}
              </details>
            )}
            {/* 数据飞轮：真实 bot 回复（非本地占位）可点赞/点踩 */}
            {!m.local && m.id > 0 && m.role === 'bot' && (
              <div className="bubble__feedback">
                <span className="bubble__feedback-hint">这条回答有帮助吗？</span>
                <button
                  className={'feedback-btn' + (m.feedback === 1 ? ' feedback-btn--active-up' : '')}
                  title="点赞（好的回答将进入人工审核池）"
                  onClick={() => handleFeedback(m.id, 1)}
                >
                  👍 有帮助
                </button>
                <button
                  className={'feedback-btn' + (m.feedback === -1 ? ' feedback-btn--active-down' : '')}
                  title="点踩（帮助我们发现差回答）"
                  onClick={() => handleFeedback(m.id, -1)}
                >
                  👎 没帮助
                </button>
              </div>
            )}
          </div>
        ))}
        {sending && <div className="bubble bubble--bot bubble--typing">正在思考…</div>}
      </div>

      <footer className="chat-page__footer">
        <button
          className="btn btn--clear"
          onClick={handleClear}
          disabled={clearing || sending}
        >
          {clearing ? '清空中…' : '清空记录'}
        </button>
        <input
          className="chat-page__input"
          value={input}
          placeholder="输入你的问题…"
          disabled={sending || clearing}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
        />
        <button className="btn btn--primary" onClick={handleSend} disabled={sending || clearing || !input.trim()}>
          {sending ? '发送中…' : '发送'}
        </button>
      </footer>
    </div>
  )
}
