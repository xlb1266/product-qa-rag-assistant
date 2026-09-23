import { useEffect, useState } from 'react'
import { documentsApi, type ChunkItem, type DocumentItem } from '../api'
import './DocumentDetailPage.css'

export default function DocumentDetailPage({
  docId,
  onBack,
}: {
  docId: number
  onBack: () => void
}) {
  const [doc, setDoc] = useState<DocumentItem | null>(null)
  const [chunks, setChunks] = useState<ChunkItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    documentsApi
      .get(docId)
      .then((data) => {
        setDoc(data.document)
        setChunks(data.chunks)
      })
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
      .finally(() => setLoading(false))
  }, [docId])

  return (
    <div className="doc-page">
      <div className="doc-page__toolbar">
        <button className="btn btn--link" onClick={onBack}>
          ← 返回列表
        </button>
      </div>

      {loading && <p className="doc-page__empty">加载中…</p>}
      {error && <p className="doc-page__empty doc-page__empty--err">{error}</p>}

      {doc && (
        <>
          <section className="doc-page__meta">
            <h2>{doc.title}</h2>
            <div className="doc-page__meta-grid">
              <span>文件名：{doc.filename}</span>
              <span>分块数：{doc.chunk_count}</span>
              <span>大小：{(doc.size / 1024).toFixed(1)} KB</span>
              <span>上传时间：{doc.created_at}</span>
            </div>
            {doc.status === 'failed' && doc.error && (
              <p className="doc-page__error">入库失败原因：{doc.error}</p>
            )}
          </section>

          <section className="doc-page__chunks">
            <h3>分块列表（{chunks.length}）</h3>
            {chunks.map((chunk) => (
              <article key={chunk.id} className="doc-page__chunk">
                <header className="doc-page__chunk-head">
                  <span className="doc-page__chunk-seq">#{chunk.seq}</span>
                  <span className="doc-page__chunk-path">{chunk.heading_path || '（无标题正文）'}</span>
                  <span className="doc-page__chunk-faiss">FAISS ID: {chunk.id}</span>
                </header>
                <pre className="doc-page__chunk-content">{chunk.content}</pre>
              </article>
            ))}
          </section>
        </>
      )}
    </div>
  )
}
