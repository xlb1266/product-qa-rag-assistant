import { useState } from 'react'
import { authApi, setToken, AuthError, UserInfo } from '../api'
import './LoginPage.css'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const PASSWORD_RE = /^\d{6}$/

interface LoginPageProps {
  onLoginSuccess: (user: UserInfo) => void
}

type Mode = 'login' | 'register'

export default function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const switchMode = (next: Mode) => {
    setMode(next)
    setError('')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return

    // 前端格式校验：邮箱格式 + 密码 6 位数字
    if (!EMAIL_RE.test(email.trim())) {
      setError('请输入正确的邮箱格式')
      return
    }
    if (!PASSWORD_RE.test(password)) {
      setError('密码必须为 6 位数字')
      return
    }

    setError('')
    setSubmitting(true)
    try {
      const res =
        mode === 'login'
          ? await authApi.login(email.trim(), password)
          : await authApi.register(email.trim(), password)
      setToken(res.token)
      onLoginSuccess(res.user)
    } catch (err) {
      if (err instanceof AuthError) {
        setError('登录已过期，请重新登录')
      } else {
        setError(err instanceof Error ? err.message : mode === 'login' ? '登录失败，请稍后重试' : '注册失败，请稍后重试')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={handleSubmit}>
        <h1 className="login-card__title">产品咨询智能客服</h1>
        <p className="login-card__subtitle">
          {mode === 'login' ? '请登录后开始咨询' : '注册新账号（默认普通用户）'}
        </p>

        {error && <div className="login-error">{error}</div>}

        <div className="login-field">
          <label className="login-field__label">邮箱</label>
          <input
            className="login-field__input"
            type="email"
            value={email}
            placeholder="请输入邮箱"
            autoComplete="email"
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>

        <div className="login-field">
          <label className="login-field__label">密码</label>
          <input
            className="login-field__input"
            type="password"
            value={password}
            placeholder="请输入 6 位数字密码"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <button className="login-submit" type="submit" disabled={submitting}>
          {submitting ? (mode === 'login' ? '登录中…' : '注册中…') : mode === 'login' ? '登录' : '注册并登录'}
        </button>

        <div className="login-switch">
          {mode === 'login' ? (
            <>
              还没有账号？
              <button type="button" className="login-switch__btn" onClick={() => switchMode('register')}>
                注册新用户
              </button>
            </>
          ) : (
            <>
              已有账号？
              <button type="button" className="login-switch__btn" onClick={() => switchMode('login')}>
                返回登录
              </button>
            </>
          )}
        </div>

        <p className="login-hint">测试账号：admin@app.com / user@app.com，密码均为 123456</p>
      </form>
    </div>
  )
}
