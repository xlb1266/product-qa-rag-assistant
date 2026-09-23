import { useEffect, useRef, useState } from 'react'
import { documentsApi, type DocumentItem } from '../api'
import './KnowledgeBasePage.css'

const STATUS_TEXT: Record<DocumentItem['status'], string> = {
  processing: '处理中',
  ready: '已入库',
  failed: '失败',
}

export default function KnowledgeBasePage({ onOpenDoc }: { onOpenDoc: (id: number) => void }) {
  const [docs, setDocs] = useState<DocumentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [message, setMessage] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const refresh = async () => {
    try {
      setDocs(await documentsApi.list())
    } catch (e) {
      setMessage({ type: 'err', text: e instanceof Error ? e.message : '加载列表失败' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setMessage({ type: 'err', text: '请先选择 Markdown 文件' })
      return
    }
    setUploading(true)
    setMessage(null)
    try {
      const doc = await documentsApi.upload(file)
      setMessage({ type: 'ok', text: `《${doc.title}》上传成功，切分为 ${doc.chunk_count} 块并已向量化入库` })
      await refresh()
    } catch (e) {
      setMessage({ type: 'err', text: e instanceof Error ? e.message : '上传失败' })
      await refresh() // 失败的文档也会留在列表中，便于查看错误原因
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const handleDelete = async (doc: DocumentItem) => {
    if (!window.confirm(`确定删除《${doc.title}》及其 ${doc.chunk_count} 个分块吗？`)) return
    try {
      await documentsApi.remove(doc.id)
      setMessage({ type: 'ok', text: `《${doc.title}》已删除` })
      await refresh()
    } catch (e) {
      setMessage({ type: 'err', text: e instanceof Error ? e.message : '删除失败' })
    }
  }

  return (
    <div className="kb-page">
      <section className="kb-page__upload">
        <div className="kb-page__upload-left">
          <h2>上传知识文档</h2>
          <p className="kb-page__hint">支持 .md 文件：按标题层级分块 → text-embedding-v4 向量化 → FAISS 入库</p>
        </div>
        <div className="kb-page__upload-right">
          <input ref={fileRef} type="file" accept=".md,.markdown" className="kb-page__file" />
          <button className="btn btn--primary" disabled={uploading} onClick={handleUpload}>
            {uploading ? '上传处理中…' : '上传文档'}
          </button>
        </div>
      </section>

      {message && (
        <div className={`kb-page__msg kb-page__msg--${message.type}`}>{message.text}</div>
      )}

      <section className="kb-page__list">
        <h2>文档列表（{docs.length}）</h2>
        {loading ? (
          <p className="kb-page__empty">加载中…</p>
        ) : docs.length === 0 ? (
          <p className="kb-page__empty">还没有文档，先上传一个 Markdown 文件吧</p>
        ) : (
          <table className="kb-page__table">
            <thead>
              <tr>
                <th>标题</th>
                <th>文件名</th>
                <th>分块数</th>
                <th>大小</th>
                <th>状态</th>
                <th>上传时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((doc) => (
                <tr key={doc.id} className={doc.status === 'failed' ? 'kb-page__row--failed' : ''}>
                  <td className="kb-page__title-cell" title={doc.error ?? doc.title}>
                    {doc.title}
                  </td>
                  <td className="kb-page__soft">{doc.filename}</td>
                  <td>{doc.chunk_count}</td>
                  <td className="kb-page__soft">{(doc.size / 1024).toFixed(1)} KB</td>
                  <td>
                    <span className={`badge badge--${doc.status}`}>{STATUS_TEXT[doc.status]}</span>
                  </td>
                  <td className="kb-page__soft">{doc.created_at}</td>
                  <td className="kb-page__actions">
                    <button className="btn btn--link" onClick={() => onOpenDoc(doc.id)}>
                      查看分块
                    </button>
                    <button className="btn btn--link btn--danger" onClick={() => handleDelete(doc)}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
