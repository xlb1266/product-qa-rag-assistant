import { useEffect, useState } from 'react'
import { authApi, clearToken, getToken, UserInfo } from './api'
import ChatPage from './pages/ChatPage'
import DebugPage from './pages/DebugPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import KnowledgeBasePage from './pages/KnowledgeBasePage'
import LogDetailPage from './pages/LogDetailPage'
import LogsPage from './pages/LogsPage'
import LoginPage from './pages/LoginPage'
import UsersPage from './pages/UsersPage'
import './App.css'

type View =
  | { name: 'kb' }
  | { name: 'chat' }
  | { name: 'log' }
  | { name: 'logDetail'; logId: number }
  | { name: 'doc'; docId: number }
  | { name: 'users' }
  | { name: 'debug' }
type AuthState = 'loading' | 'guest' | 'authed'

export default function App() {
  const [authState, setAuthState] = useState<AuthState>('loading')
  const [user, setUser] = useState<UserInfo | null>(null)
  // 登录成功后默认进入会话页（在线咨询）
  const [view, setView] = useState<View>({ name: 'chat' })

  // 启动时校验本地 token：有效则进入应用，无效/无 token 则回登录页
  useEffect(() => {
    const token = getToken()
    if (!token) {
      setAuthState('guest')
      return
    }
    authApi
      .me()
      .then((u) => {
        setUser(u)
        setAuthState('authed')
      })
      .catch(() => {
        clearToken()
        setAuthState('guest')
      })
  }, [])

  const handleLoginSuccess = (u: UserInfo) => {
    setUser(u)
    setView({ name: 'chat' })
    setAuthState('authed')
  }

  const handleLogout = () => {
    clearToken()
    setUser(null)
    setAuthState('guest')
  }

  if (authState === 'loading') {
    return (
      <div className="app__loading">正在检查登录状态…</div>
    )
  }

  // 未登录：只展示登录页
  if (authState === 'guest' || !user) {
    return <LoginPage onLoginSuccess={handleLoginSuccess} />
  }

  const isKb = view.name === 'kb'
  const isChat = view.name === 'chat'
  const isLog = view.name === 'log'
  const isUsers = view.name === 'users'
  const isDebug = view.name === 'debug'
  const isAdmin = user.role === 'admin'

  // 已登录：展示主应用。管理类页面（知识库/用户管理）只对管理员显示；
  // 隐藏入口只是体验层，后端接口有独立的角色校验兜底。
  return (
    <div className="app">
      <header className="app__header">
        <h1>产品咨询智能客服</h1>
        <nav className="app__tabs">
          {isAdmin && (
            <button
              className={`app__tab ${isKb ? 'app__tab--active' : ''}`}
              onClick={() => setView({ name: 'kb' })}
            >
              知识库管理
            </button>
          )}
          <button
            className={`app__tab ${isChat ? 'app__tab--active' : ''}`}
            onClick={() => setView({ name: 'chat' })}
          >
            在线咨询
          </button>
          {isAdmin && (
            <button
              className={`app__tab ${isLog ? 'app__tab--active' : ''}`}
              onClick={() => setView({ name: 'log' })}
            >
              日志查看
            </button>
          )}
          {isAdmin && (
            <button
              className={`app__tab ${isUsers ? 'app__tab--active' : ''}`}
              onClick={() => setView({ name: 'users' })}
            >
              用户管理
            </button>
          )}
          {isAdmin && (
            <button
              className={`app__tab ${isDebug ? 'app__tab--active' : ''}`}
              onClick={() => setView({ name: 'debug' })}
            >
              调试中心
            </button>
          )}
        </nav>
        <div className="app__header-right">
          <span className="app__user-email">
            {user.email}
            {isAdmin ? ' · 管理员' : ''}
          </span>
          <button className="app__logout" onClick={handleLogout}>
            退出登录
          </button>
        </div>
      </header>

      {isAdmin && view.name === 'kb' && <KnowledgeBasePage onOpenDoc={(id) => setView({ name: 'doc', docId: id })} />}
      {isAdmin && view.name === 'doc' && (
        <DocumentDetailPage docId={view.docId} onBack={() => setView({ name: 'kb' })} />
      )}
      {view.name === 'chat' && <ChatPage />}
      {isAdmin && view.name === 'log' && (
        <LogsPage onOpenLog={(id) => setView({ name: 'logDetail', logId: id })} />
      )}
      {isAdmin && view.name === 'logDetail' && (
        <LogDetailPage logId={view.logId} onBack={() => setView({ name: 'log' })} />
      )}
      {isAdmin && view.name === 'users' && <UsersPage currentUserEmail={user.email} />}
      {isAdmin && view.name === 'debug' && (
        <DebugPage onOpenLog={(id) => setView({ name: 'logDetail', logId: id })} />
      )}
    </div>
  )
}
