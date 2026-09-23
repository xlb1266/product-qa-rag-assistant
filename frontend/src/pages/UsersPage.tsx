import { useEffect, useState } from 'react'
import { RoleChangeItem, SecurityLogItem, UserItem, UserRole, usersApi } from '../api'
import './UsersPage.css'

interface UsersPageProps {
  currentUserEmail: string
}

/** 用户管理页（仅管理员可见）：用户列表 + 角色调整（密码二次验证）+ 变更留痕 + 安全事件 */
export default function UsersPage({ currentUserEmail }: UsersPageProps) {
  const [users, setUsers] = useState<UserItem[]>([])
  const [roleChanges, setRoleChanges] = useState<RoleChangeItem[]>([])
  const [securityLogs, setSecurityLogs] = useState<SecurityLogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 角色变更弹层状态
  const [editing, setEditing] = useState<UserItem | null>(null)
  const [adminPassword, setAdminPassword] = useState('')
  const [changing, setChanging] = useState(false)
  const [changeError, setChangeError] = useState('')
  const [notice, setNotice] = useState('')

  const reloadAll = () => {
    Promise.all([usersApi.list(), usersApi.roleChanges(), usersApi.securityLogs()])
      .then(([u, rc, sl]) => {
        setUsers(u)
        setRoleChanges(rc)
        setSecurityLogs(sl)
      })
      .catch((e) => setError(e instanceof Error ? e.message : '加载失败'))
  }

  useEffect(() => {
    reloadAll()
    setLoading(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const openEditor = (u: UserItem) => {
    setEditing(u)
    setAdminPassword('')
    setChangeError('')
  }

  const submitRoleChange = async (role: UserRole) => {
    if (!editing || changing) return
    if (!/^\d{6}$/.test(adminPassword)) {
      setChangeError('请输入管理员 6 位数字密码')
      return
    }
    setChanging(true)
    setChangeError('')
    try {
      const res = await usersApi.changeRole(editing.id, role, adminPassword)
      setNotice('已把 ' + res.email + ' 从 ' + res.old_role + ' 改为 ' + res.new_role)
      setEditing(null)
      setAdminPassword('')
      reloadAll()
    } catch (e) {
      setChangeError(e instanceof Error ? e.message : '变更失败')
    } finally {
      setChanging(false)
    }
  }

  if (loading) {
    return <div className="users-page"><div className="users-page__empty">正在加载用户…</div></div>
  }
  if (error) {
    return <div className="users-page"><div className="users-page__empty">{error}</div></div>
  }

  return (
    <div className="users-page">
      <h2>用户管理</h2>
      {notice && (
        <div className="users-page__notice">
          {notice}
          <button className="users-page__notice-close" onClick={() => setNotice('')}>×</button>
        </div>
      )}
      <table className="users-page__table">
        <thead>
          <tr>
            <th>邮箱</th>
            <th>角色</th>
            <th>注册时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td className="users-page__email">{u.email}{u.email === currentUserEmail ? '（我）' : ''}</td>
              <td>
                <span className={'role-tag role-tag--' + u.role}>
                  {u.role === 'admin' ? '管理员' : '普通用户'}
                </span>
              </td>
              <td className="users-page__soft">{u.created_at}</td>
              <td>
                {u.email === currentUserEmail ? (
                  <span className="users-page__soft">-</span>
                ) : u.role === 'admin' ? (
                  // 管理员不能降级其他管理员，不显示降级按钮
                  <span className="users-page__soft" title="管理员不能被其他管理员降级">不可降级</span>
                ) : (
                  <button className="users-page__action" onClick={() => openEditor(u)}>
                    升为管理员
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editing && (
        <div className="users-page__dialog">
          <div className="users-page__dialog-card">
            <h3>调整 {editing.email} 的角色</h3>
            <p className="users-page__dialog-hint">
              当前角色：{editing.role === 'admin' ? '管理员' : '普通用户'}。
              角色变更是关键操作，请输入<b>你的管理员密码</b>进行二次验证。
            </p>
            <input
              className="users-page__dialog-input"
              type="password"
              placeholder="管理员密码（6 位数字）"
              value={adminPassword}
              autoComplete="current-password"
              onChange={(e) => setAdminPassword(e.target.value)}
            />
            {changeError && <div className="users-page__dialog-error">{changeError}</div>}
            <div className="users-page__dialog-actions">
              <button className="users-page__btn users-page__btn--ghost" onClick={() => setEditing(null)} disabled={changing}>
                取消
              </button>
              <button
                className="users-page__btn users-page__btn--primary"
                onClick={() => submitRoleChange(editing.role === 'admin' ? 'user' : 'admin')}
                disabled={changing}
              >
                {changing ? '提交中…' : editing.role === 'admin' ? '确认降为普通用户' : '确认升为管理员'}
              </button>
            </div>
          </div>
        </div>
      )}

      <h2>角色变更历史</h2>
      {roleChanges.length === 0 ? (
        <div className="users-page__empty">暂无角色变更记录</div>
      ) : (
        <table className="users-page__table">
          <thead>
            <tr>
              <th>时间</th>
              <th>操作人</th>
              <th>对象</th>
              <th>变更</th>
            </tr>
          </thead>
          <tbody>
            {roleChanges.map((rc) => (
              <tr key={rc.id}>
                <td className="users-page__soft">{rc.created_at}</td>
                <td>{rc.operator_email}</td>
                <td>{rc.target_email}</td>
                <td>
                  <span className={'role-tag role-tag--' + rc.old_role}>{rc.old_role}</span>
                  <span className="users-page__arrow">→</span>
                  <span className={'role-tag role-tag--' + rc.new_role}>{rc.new_role}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>安全事件（越权拦截记录）</h2>
      {securityLogs.length === 0 ? (
        <div className="users-page__empty">暂无安全事件</div>
      ) : (
        <table className="users-page__table">
          <thead>
            <tr>
              <th>时间</th>
              <th>用户</th>
              <th>目标接口</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            {securityLogs.map((sl) => (
              <tr key={sl.id}>
                <td className="users-page__soft">{sl.created_at}</td>
                <td>{sl.user_email}</td>
                <td className="users-page__mono">{sl.path}</td>
                <td className="users-page__soft">{sl.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
