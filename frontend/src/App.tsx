import React, { useState, useEffect, useRef } from 'react';
import { 
  Database, 
  MessageSquare, 
  Sparkles, 
  Upload, 
  Plus, 
  ArrowUp, 
  Sliders, 
  Shield, 
  LogOut,
  Layers, 
  Cpu, 
  FileText, 
  Check, 
  Copy,
  Zap,
  Globe,
  RefreshCw,
  CheckCircle2,
  FileCheck,
  Trash2,
  Loader2,
  AlertCircle,
  Lock
} from 'lucide-react';
import { ApiClient } from './services/api';

export default function App() {
  const [token, setToken] = useState<string | null>(ApiClient.getToken());
  const [profile, setProfile] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<'chat' | 'kb' | 'config'>('chat');

  // Auth form
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('password123');
  const [tenantName, setTenantName] = useState('default');
  const [loginError, setLoginError] = useState('');
  const [loadingAuth, setLoadingAuth] = useState(false);

  // Knowledge Bases & Selection
  const [kbs, setKbs] = useState<any[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string>('');
  const [sessionId, setSessionId] = useState<string>(() => crypto.randomUUID());
  const [newKbName, setNewKbName] = useState('');
  const [newKbDesc, setNewKbDesc] = useState('');

  // Documents State
  const [documents, setDocuments] = useState<any[]>([]);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const pollTimerRef = useRef<any>(null);

  // Chat State
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState('hybrid');
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentStatus, setCurrentStatus] = useState('');
  const [streamingAnswer, setStreamingAnswer] = useState('');
  const [messages, setMessages] = useState<any[]>([]);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  // Config State
  const [tenantConfig, setTenantConfig] = useState<any>(null);
  const [tempTemperature, setTempTemperature] = useState(0.3);
  const [tempTopK, setTempTopK] = useState(5);
  const [tempPersona, setTempPersona] = useState('');
  const [configSuccess, setConfigSuccess] = useState('');

  // Upload State
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadStatus, setUploadStatus] = useState('');
  const chatEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (token) {
      loadInitialData();
    }
  }, [token]);

  useEffect(() => {
    if (selectedKbId && token) {
      loadDocuments(selectedKbId);
      setSessionId(crypto.randomUUID());
      setMessages([]);
    }
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [selectedKbId, token]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [streamingAnswer, messages]);

  const loadDocuments = async (kbId: string) => {
    setLoadingDocs(true);
    try {
      const docs = await ApiClient.getDocuments(kbId);
      setDocuments(docs);

      // Auto-poll if any document is currently being ingested
      const hasActive = docs.some((d: any) => d.task_status === 'PENDING' || d.task_status === 'RUNNING');
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
      if (hasActive) {
        pollTimerRef.current = setTimeout(() => {
          loadDocuments(kbId);
        }, 2500);
      }
    } catch (e: any) {
      console.error(e);
    } finally {
      setLoadingDocs(false);
    }
  };

  const loadInitialData = async () => {
    try {
      const userProfile = await ApiClient.getProfile();
      setProfile(userProfile);

      const kbList = await ApiClient.getKnowledgeBases();
      setKbs(kbList);
      if (kbList.length > 0) {
        setSelectedKbId(kbList[0].id);
        loadDocuments(kbList[0].id);
      }

      const cfg = await ApiClient.getTenantConfig();
      setTenantConfig(cfg);
      setTempTemperature(cfg.temperature);
      setTempTopK(cfg.top_k);
      setTempPersona(cfg.system_persona || '');
    } catch (e: any) {
      console.error(e);
      const refreshed = await ApiClient.tryRefresh();
      if (refreshed) {
        setToken(ApiClient.getToken());
      } else {
        ApiClient.clearToken();
        setToken(null);
      }
    }
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError('');
    setLoadingAuth(true);
    try {
      const data = await ApiClient.login(username, password, tenantName);
      setToken(data.access_token);
    } catch (err: any) {
      setLoginError(err.message || '账号或密码错误');
    } finally {
      setLoadingAuth(false);
    }
  };

  const setQuickAccount = (u: string, p: string, t: string) => {
    setUsername(u);
    setPassword(p);
    setTenantName(t);
  };

  const handleLogout = async () => {
    await ApiClient.logout();
    setToken(null);
    setProfile(null);
  };

  const handleCreateKb = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newKbName.trim()) return;
    try {
      const created = await ApiClient.createKnowledgeBase(newKbName, newKbDesc);
      setKbs([...kbs, created]);
      setSelectedKbId(created.id);
      setNewKbName('');
      setNewKbDesc('');
    } catch (err: any) {
      alert(err.message);
    }
  };

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile || !selectedKbId) return;
    setUploadStatus('正在校验并安全摄取...');
    try {
      const res = await ApiClient.uploadDocument(selectedKbId, uploadFile);
      const taskId = res.task?.id || res.task_id || res.document?.id || '';
      const displayId = taskId ? `，任务 ID: ${taskId.slice(0, 8)}...` : '';
      setUploadStatus(`已成功入库并创建解析任务${displayId}`);
      setUploadFile(null);
      // Reload document list and trigger polling
      loadDocuments(selectedKbId);
    } catch (err: any) {
      setUploadStatus(`上传失败: ${err.message}`);
    }
  };

  const handleDeleteDoc = async (docId: string, filename: string) => {
    if (!confirm(`确定要删除文档 "${filename}" 吗？`)) return;
    try {
      await ApiClient.deleteDocument(docId);
      if (selectedKbId) loadDocuments(selectedKbId);
    } catch (e: any) {
      alert(`删除失败: ${e.message}`);
    }
  };

  const handleReingestDoc = async (docId: string, filename: string) => {
    try {
      await ApiClient.reingestDocument(docId);
      setUploadStatus(`已为「${filename}」重新创建索引任务`);
      if (selectedKbId) loadDocuments(selectedKbId);
    } catch (e: any) {
      alert(`重新处理失败: ${e.message}`);
    }
  };

  const handleUpdateConfig = async () => {
    setConfigSuccess('');
    try {
      const updated = await ApiClient.updateTenantConfig({
        temperature: tempTemperature,
        top_k: tempTopK,
        system_persona: tempPersona,
      });
      setTenantConfig(updated);
      setConfigSuccess(`配置已更新至版本 V${updated.version_id}`);
    } catch (err: any) {
      alert(err.message);
    }
  };

  const handleCopy = (text: string, index: number) => {
    navigator.clipboard.writeText(text);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const formatBytes = (bytes: number) => {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  };

  const handleSendChat = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim() || isStreaming || !selectedKbId) return;

    const userQ = query;
    setQuery('');
    setMessages(prev => [...prev, { role: 'user', content: userQ }]);
    setIsStreaming(true);
    setStreamingAnswer('');
    setCurrentStatus('检索向量与图谱中...');

    let accumulatedAnswer = '';
    let accumulatedSources: any[] = [];
    let accumulatedEntities: any[] = [];
    const activeTopK = tempTopK || tenantConfig?.top_k || 8;

    await ApiClient.streamQueryRAG(
      selectedKbId,
      userQ,
      mode,
      activeTopK,
      (status) => setCurrentStatus(status),
      (srcs, ents) => {
        accumulatedSources = srcs;
        accumulatedEntities = ents;
      },
      (delta) => {
        accumulatedAnswer += delta;
        setStreamingAnswer(accumulatedAnswer);
      },
      (doneMetrics) => {
        setIsStreaming(false);
        setCurrentStatus('');
        setMessages(prev => [
          ...prev, 
          { 
            role: 'assistant', 
            content: accumulatedAnswer,
            sources: accumulatedSources,
            entities: accumulatedEntities,
            metrics: doneMetrics 
          }
        ]);
        setStreamingAnswer('');
      },
      (error) => {
        setIsStreaming(false);
        setCurrentStatus(`错误: ${error}`);
      },
      sessionId,
    );
  };

  // --- MINIMALIST ELEGANT LIGHT LOGIN SCREEN ---
  if (!token) {
    return (
      <div style={{ height: '100vh', width: '100vw', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-app)', position: 'relative' }}>
        <div className="ambient-glow" />
        <div className="card-clean" style={{ width: 380, padding: 36, position: 'relative', zIndex: 1, boxShadow: 'var(--shadow-elevated)' }}>
          <div style={{ marginBottom: 28 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <div style={{ width: 32, height: 32, borderRadius: 8, background: 'var(--accent-primary)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 6px rgba(79, 70, 229, 0.3)' }}>
                <Zap size={18} color="#fff" />
              </div>
              <span style={{ fontSize: 16, fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>GraphRAG Pro</span>
            </div>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)' }}>企业级多模态知识图谱平台</p>
          </div>

          <form onSubmit={handleLogin} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {loginError && (
              <div style={{ padding: '8px 12px', borderRadius: 6, background: '#fef2f2', border: '1px solid #fee2e2', color: '#dc2626', fontSize: 12 }}>
                {loginError}
              </div>
            )}

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 5, color: 'var(--text-secondary)' }}>所属企业/租户空间</label>
              <input 
                className="input-minimal" 
                type="text" 
                value={tenantName} 
                onChange={(e) => setTenantName(e.target.value)} 
                placeholder="例如: default 或 alpha"
                required 
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 5, color: 'var(--text-secondary)' }}>账号 (Username)</label>
              <input 
                className="input-minimal" 
                type="text" 
                value={username} 
                onChange={(e) => setUsername(e.target.value)} 
                required 
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 12, fontWeight: 500, marginBottom: 5, color: 'var(--text-secondary)' }}>密码 (Password)</label>
              <input 
                className="input-minimal" 
                type="password" 
                value={password} 
                onChange={(e) => setPassword(e.target.value)} 
                required 
              />
            </div>

            <button type="submit" className="btn-solid" disabled={loadingAuth} style={{ width: '100%', justifyContent: 'center', marginTop: 10, padding: '10px 0' }}>
              {loadingAuth ? '正在验证...' : '登录系统'}
            </button>
          </form>

          {/* QUICK ACCOUNT SWITCHER CHIPS */}
          <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid var(--border-subtle)' }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)', marginBottom: 8, textAlign: 'center' }}>
              快速填入预设权限账号 (密码: password123)
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
              <button 
                type="button" 
                onClick={() => setQuickAccount('admin', 'password123', 'default')}
                className="btn-ghost" 
                style={{ fontSize: 11, padding: '6px 8px', justifyContent: 'flex-start', border: '1px solid var(--border-subtle)', background: '#fff' }}
              >
                <Shield size={12} color="#4f46e5" />
                <span>admin (租户管理员)</span>
              </button>
              <button 
                type="button" 
                onClick={() => setQuickAccount('member', 'password123', 'default')}
                className="btn-ghost" 
                style={{ fontSize: 11, padding: '6px 8px', justifyContent: 'flex-start', border: '1px solid var(--border-subtle)', background: '#fff' }}
              >
                <Zap size={12} color="#059669" />
                <span>member (业务专员)</span>
              </button>
              <button 
                type="button" 
                onClick={() => setQuickAccount('viewer', 'password123', 'default')}
                className="btn-ghost" 
                style={{ fontSize: 11, padding: '6px 8px', justifyContent: 'flex-start', border: '1px solid var(--border-subtle)', background: '#fff' }}
              >
                <Lock size={12} color="#d97706" />
                <span>viewer (只读访客)</span>
              </button>
              <button 
                type="button" 
                onClick={() => setQuickAccount('alpha_admin', 'password123', 'alpha')}
                className="btn-ghost" 
                style={{ fontSize: 11, padding: '6px 8px', justifyContent: 'flex-start', border: '1px solid #c7d2fe', background: '#eef2ff' }}
                title="登录独立的 Alpha 租户管理员账号"
              >
                <Globe size={12} color="#4338ca" />
                <span>alpha_admin (Alpha隔离租户)</span>
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // --- MINIMALIST LIGHT WORKSPACE DASHBOARD ---
  return (
    <div style={{ display: 'flex', height: '100vh', width: '100vw', background: 'var(--bg-app)' }}>
      {/* MINIMALIST LIGHT SIDEBAR */}
      <div style={{ width: 230, background: 'var(--bg-sidebar)', borderRight: '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', zIndex: 2 }}>
        <div style={{ padding: '18px 16px', display: 'flex', alignItems: 'center', gap: 10 }}>
          <div style={{ width: 26, height: 26, borderRadius: 6, background: 'var(--accent-primary)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 4px rgba(79, 70, 229, 0.2)' }}>
            <Zap size={15} color="#fff" />
          </div>
          <span style={{ fontSize: 14, fontWeight: 700, letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>GraphRAG</span>
          <span className="pill pill-default" style={{ fontSize: 10, padding: '1px 6px' }}>v0.1</span>
        </div>

        {/* NAVIGATION LINKS */}
        <div style={{ padding: '6px 10px', display: 'flex', flexDirection: 'column', gap: 3, flex: 1 }}>
          <button 
            onClick={() => setActiveTab('chat')} 
            className={`btn-ghost ${activeTab === 'chat' ? 'active' : ''}`}
            style={{ width: '100%', justifyContent: 'flex-start' }}
          >
            <MessageSquare size={16} />
            <span>智能问答</span>
          </button>

          <button 
            onClick={() => setActiveTab('kb')} 
            className={`btn-ghost ${activeTab === 'kb' ? 'active' : ''}`}
            style={{ width: '100%', justifyContent: 'flex-start' }}
          >
            <Database size={16} />
            <span>知识库管理</span>
          </button>

          <button 
            onClick={() => setActiveTab('config')} 
            className={`btn-ghost ${activeTab === 'config' ? 'active' : ''}`}
            style={{ width: '100%', justifyContent: 'flex-start' }}
          >
            <Sliders size={16} />
            <span>模型与 Prompt</span>
          </button>
        </div>

        {/* PROFILE & LOGOUT */}
        <div style={{ padding: '14px 16px', borderTop: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: 'rgba(255, 255, 255, 0.4)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{ width: 28, height: 28, borderRadius: '50%', background: '#ffffff', border: '1px solid var(--border-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, fontWeight: 600, color: 'var(--accent-primary)' }}>
              {profile?.username?.[0]?.toUpperCase() || 'A'}
            </div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>{profile?.username}</div>
              <div style={{ fontSize: 10, color: 'var(--text-tertiary)' }}>{profile?.role}</div>
            </div>
          </div>
          <button onClick={handleLogout} className="btn-ghost" style={{ padding: 6, color: 'var(--text-tertiary)' }} title="退出登录">
            <LogOut size={14} />
          </button>
        </div>
      </div>

      {/* MAIN VIEWPORT */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
        {/* TOP HEADER BAR */}
        <div style={{ height: 52, borderBottom: '1px solid var(--border-subtle)', padding: '0 24px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', background: '#ffffff' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>知识空间</span>
            <select 
              className="input-minimal" 
              style={{ width: 200, padding: '4px 10px', fontSize: 12, height: 30 }}
              value={selectedKbId}
              onChange={(e) => setSelectedKbId(e.target.value)}
            >
              {kbs.map(k => (
                <option key={k.id} value={k.id}>{k.name}</option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span className="pill pill-default" title={`实时生效检索设置: Top-${tempTopK}, 生成温度: ${tempTemperature}`}>
              🎯 Top-{tempTopK} | 🌡️ {tempTemperature} (V{tenantConfig?.version_id || 1})
            </span>
            <span className="pill pill-success">
              <Shield size={11} /> RBAC Protected
            </span>
          </div>
        </div>

        {/* TAB 1: ELEGANT LIGHT CHAT VIEW */}
        {activeTab === 'chat' && (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', maxWidth: 840, width: '100%', margin: '0 auto', padding: '16px 24px', overflow: 'hidden' }}>
            {/* RETRIEVAL MODE SEGMENTED CONTROL */}
            <div style={{ marginBottom: 14, display: 'flex', justifyContent: 'center' }}>
              <div className="segmented-control">
                {[
                  { id: 'hybrid', label: 'Mix 全能', icon: Layers },
                  { id: 'naive', label: '向量检索', icon: FileText },
                  { id: 'local', label: '子图检索', icon: Cpu },
                  { id: 'global', label: '全局摘要', icon: Globe },
                ].map(m => (
                  <button
                    key={m.id}
                    onClick={() => setMode(m.id)}
                    className={`segmented-item ${mode === m.id ? 'active' : ''}`}
                  >
                    <m.icon size={13} />
                    {m.label}
                  </button>
                ))}
              </div>
            </div>

            {/* MESSAGES FLOW */}
            <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 16, paddingRight: 4 }}>
              {messages.length === 0 && !isStreaming && (
                <div style={{ textAlign: 'center', margin: 'auto', color: 'var(--text-tertiary)' }}>
                  <Sparkles size={28} style={{ margin: '0 auto 12px', strokeWidth: 1.5, color: 'var(--accent-primary)' }} />
                  <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 4 }}>
                    已就绪，请输入您的问题
                  </div>
                  <div style={{ fontSize: 12 }}>
                    支持多路融合检索、实体关系子图溯源与打字机流式回答
                  </div>
                </div>
              )}

              {messages.map((msg, i) => (
                <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: msg.role === 'user' ? 'flex-end' : 'flex-start', gap: 6 }}>
                  <div 
                    style={{ 
                      maxWidth: '88%',
                      padding: msg.role === 'user' ? '10px 16px' : '14px 18px',
                      borderRadius: 'var(--radius-md)',
                      background: msg.role === 'user' ? '#f1f4f8' : '#ffffff',
                      border: '1px solid var(--border-subtle)',
                      boxShadow: msg.role === 'user' ? 'none' : 'var(--shadow-subtle)',
                      fontSize: 13.5,
                      lineHeight: 1.6,
                      whiteSpace: 'pre-wrap',
                      position: 'relative',
                      color: 'var(--text-primary)'
                    }}
                  >
                    {msg.content}

                    {/* COPY BUTTON */}
                    {msg.role === 'assistant' && (
                      <button 
                        onClick={() => handleCopy(msg.content, i)} 
                        style={{ position: 'absolute', top: 10, right: 10, background: 'transparent', border: 'none', color: 'var(--text-tertiary)', cursor: 'pointer' }}
                        title="复制回答"
                      >
                        {copiedIndex === i ? <Check size={14} color="#059669" /> : <Copy size={14} />}
                      </button>
                    )}

                    {/* CITATION SOURCES BADGES */}
                    {msg.sources && msg.sources.length > 0 && (
                      <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-secondary)' }}>引用依据与相关度:</div>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                          {msg.sources.map((s: any, sIdx: number) => (
                            <span 
                              key={sIdx} 
                              className="pill pill-default" 
                              style={{ fontSize: 11, background: '#f8fafc', display: 'inline-flex', alignItems: 'center', gap: 6 }}
                              title={`切片 #${s.chunk_index}${s.page_number ? ` (第 ${s.page_number} 页)` : ''}\n相关度: ${(s.score * 100).toFixed(1)}%`}
                            >
                              <span>{s.filename}</span>
                              {s.page_number && (
                                <span style={{ background: '#e0e7ff', color: '#3730a3', padding: '1px 5px', borderRadius: 4, fontWeight: 700, fontSize: 10 }}>
                                  第{s.page_number}页
                                </span>
                              )}
                              <span style={{ color: 'var(--accent-primary)', fontWeight: 600 }}>{(s.score * 100).toFixed(0)}%</span>
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {/* STREAMING IN PROGRESS */}
              {isStreaming && (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}>
                  <div 
                    style={{ 
                      maxWidth: '88%',
                      padding: '14px 18px',
                      borderRadius: 'var(--radius-md)',
                      background: '#ffffff',
                      border: '1px solid var(--border-subtle)',
                      boxShadow: 'var(--shadow-subtle)',
                      fontSize: 13.5,
                      lineHeight: 1.6,
                      whiteSpace: 'pre-wrap',
                      color: 'var(--text-primary)'
                    }}
                  >
                    {currentStatus && (
                      <div style={{ fontSize: 11, color: 'var(--accent-primary)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6, fontWeight: 500 }}>
                        <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent-primary)', display: 'inline-block' }} />
                        {currentStatus}
                      </div>
                    )}
                    {streamingAnswer}
                    <span className="cursor-blink" />
                  </div>
                </div>
              )}
              <div ref={chatEndRef} />
            </div>

            {/* MINIMALIST FLOATING INPUT BAR */}
            <form onSubmit={handleSendChat} style={{ marginTop: 14, position: 'relative' }}>
              <input
                className="input-minimal"
                type="text"
                placeholder="提问知识库中的内容..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                disabled={isStreaming}
                style={{ padding: '12px 48px 12px 16px', fontSize: 13, borderRadius: 'var(--radius-md)', boxShadow: 'var(--shadow-subtle)' }}
              />
              <button 
                type="submit" 
                disabled={isStreaming || !query.trim()}
                style={{ 
                  position: 'absolute', 
                  right: 8, 
                  top: '50%', 
                  transform: 'translateY(-50%)',
                  width: 30,
                  height: 30,
                  borderRadius: 8,
                  background: query.trim() ? 'var(--accent-primary)' : '#f1f5f9',
                  border: 'none',
                  color: query.trim() ? '#ffffff' : 'var(--text-disabled)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  cursor: query.trim() ? 'pointer' : 'default',
                  transition: 'all 0.15s ease'
                }}
              >
                <ArrowUp size={16} />
              </button>
            </form>
          </div>
        )}

        {/* TAB 2: MINIMALIST LIGHT KB & DOCUMENTS VIEW */}
        {activeTab === 'kb' && (
          <div style={{ flex: 1, overflowY: 'auto', padding: 28, maxWidth: 880, width: '100%', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 24 }}>
            {/* TOP ROW: CREATE KB & UPLOAD */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
              {/* CREATE KB */}
              <div className="card-clean" style={{ padding: 22 }}>
                <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-primary)' }}>
                  <Database size={16} color="var(--accent-primary)" />
                  新建知识库空间
                </div>
                {profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN' && (
                  <div style={{ padding: '6px 10px', borderRadius: 6, background: '#f8fafc', border: '1px solid var(--border-subtle)', color: 'var(--text-secondary)', fontSize: 11, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Lock size={12} />
                    <span>仅租户管理员有权创建新知识空间</span>
                  </div>
                )}
                <form onSubmit={handleCreateKb} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>名称</label>
                    <input className="input-minimal" type="text" placeholder="例如: 政策法规知识库" value={newKbName} onChange={(e) => setNewKbName(e.target.value)} disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'} required />
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>描述 (选填)</label>
                    <textarea className="input-minimal" rows={2} placeholder="业务范围或知识分类..." value={newKbDesc} onChange={(e) => setNewKbDesc(e.target.value)} disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'} />
                  </div>
                  <button type="submit" className="btn-solid" disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'} style={{ alignSelf: 'flex-start', marginTop: 4, opacity: (profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN') ? 0.6 : 1 }}>
                    <Plus size={14} /> 创建知识空间
                  </button>
                </form>
              </div>

              {/* UPLOAD DOCUMENT */}
              <div className="card-clean" style={{ padding: 22 }}>
                <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 14, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-primary)' }}>
                  <Upload size={16} color="var(--accent-primary)" />
                  多模态文档摄取
                </div>
                {profile?.role === 'READONLY' && (
                  <div style={{ padding: '6px 10px', borderRadius: 6, background: '#fffbeb', border: '1px solid #fde68a', color: '#b45309', fontSize: 11, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Lock size={12} />
                    <span>只读访客角色 (READONLY) 受 RBAC 保护禁止上传新文件</span>
                  </div>
                )}
                <form onSubmit={handleUpload} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>选择文件 (PDF, DOCX, TXT)</label>
                    <input 
                      className="input-minimal" 
                      type="file" 
                      accept=".pdf,.docx,.txt,.md" 
                      onChange={(e) => setUploadFile(e.target.files?.[0] || null)} 
                      disabled={profile?.role === 'READONLY'}
                      required 
                    />
                  </div>
                  {uploadStatus && (
                    <div style={{ fontSize: 12, color: 'var(--text-secondary)', padding: '6px 10px', background: '#f8fafc', borderRadius: 6, border: '1px solid var(--border-subtle)' }}>
                      {uploadStatus}
                    </div>
                  )}
                  <button 
                    type="submit" 
                    className="btn-solid" 
                    disabled={!uploadFile || profile?.role === 'READONLY'} 
                    style={{ alignSelf: 'flex-start', marginTop: 4, opacity: profile?.role === 'READONLY' ? 0.6 : 1 }}
                    title={profile?.role === 'READONLY' ? '只读访客禁止上传文档' : '开始解析入库'}
                  >
                    {profile?.role === 'READONLY' ? '只读访客禁止上传' : '开始解析入库'}
                  </button>
                </form>
              </div>
            </div>

            {/* BOTTOM ROW: DOCUMENTS LIST IN CURRENT KB */}
            <div className="card-clean" style={{ padding: 22 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <FileCheck size={18} color="var(--accent-primary)" />
                  <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>
                    当前知识空间已入库文档
                  </span>
                  <span className="pill pill-default" style={{ fontSize: 11 }}>
                    {documents.length} 篇文档
                  </span>
                </div>
                <button 
                  onClick={() => selectedKbId && loadDocuments(selectedKbId)} 
                  className="btn-ghost" 
                  style={{ padding: '4px 8px', fontSize: 12 }}
                  title="刷新文档列表"
                >
                  <RefreshCw size={13} className={loadingDocs ? "spin" : ""} /> 刷新
                </button>
              </div>

              {documents.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '36px 0', color: 'var(--text-tertiary)' }}>
                  <FileText size={32} style={{ margin: '0 auto 10px', strokeWidth: 1.2, color: 'var(--text-disabled)' }} />
                  <div style={{ fontSize: 13 }}>当前知识库尚无已入库文档</div>
                  <div style={{ fontSize: 11, marginTop: 4 }}>请在上方选择文件并点击「开始解析入库」</div>
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                  {documents.map((doc) => {
                    const isRunning = doc.task_status === 'RUNNING';
                    const isPending = ['PENDING', 'RETRY_WAITING'].includes(doc.task_status);
                    const isFailed = ['FAILED', 'DEAD_LETTER'].includes(doc.task_status);
                    const isIndexReady = doc.index_status === 'READY';
                    const isIndexPartial = doc.index_status === 'PARTIAL';
                    const isIndexMissing = doc.index_status === 'NOT_INDEXED';
                    const isIndexUnknown = doc.index_status === 'UNKNOWN';
                    const isSuccess = isIndexReady && doc.task_status === 'SUCCEEDED';

                    return (
                      <div 
                        key={doc.id} 
                        style={{ 
                          display: 'flex', 
                          flexDirection: 'column',
                          padding: '14px 16px', 
                          borderRadius: 8, 
                          background: '#f8fafc', 
                          border: '1px solid var(--border-subtle)',
                          transition: 'all 0.15s ease',
                          gap: 8
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
                            <div style={{ width: 34, height: 34, borderRadius: 8, background: 'var(--accent-soft)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--accent-primary)' }}>
                              <FileText size={18} />
                            </div>
                            <div style={{ minWidth: 0 }}>
                              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {doc.filename}
                              </div>
                              <div style={{ fontSize: 11, color: 'var(--text-tertiary)', display: 'flex', alignItems: 'center', gap: 10, marginTop: 2 }}>
                                <span>大小: {formatBytes(doc.file_size)}</span>
                                <span>•</span>
                                <span style={{ fontFamily: 'var(--font-mono)' }}>ID: {doc.id.slice(0, 8)}...</span>
                                {doc.content_hash && (
                                  <>
                                    <span>•</span>
                                    <span style={{ fontFamily: 'var(--font-mono)' }}>SHA: {doc.content_hash.slice(0, 6)}...</span>
                                  </>
                                )}
                              </div>
                            </div>
                          </div>

                          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                            {isSuccess && (
                              <span className="pill pill-success" style={{ fontSize: 11 }}>
                                <CheckCircle2 size={12} />
                                已入库且可检索 ({doc.indexed_chunks} 段)
                              </span>
                            )}
                            {isIndexPartial && (
                              <span className="pill pill-default" style={{ fontSize: 11, color: '#b45309', borderColor: '#fde68a', background: '#fffbeb' }}>
                                <AlertCircle size={12} />
                                部分内容可检索 ({doc.indexed_chunks} 段)
                              </span>
                            )}
                            {isRunning && (
                              <span className="pill pill-accent" style={{ fontSize: 11 }}>
                                <Loader2 size={12} className="spin" />
                                正在解析: {doc.task_stage || '处理中'} ({doc.task_progress || 0}%)
                              </span>
                            )}
                            {isPending && (
                              <span className="pill pill-default" style={{ fontSize: 11, color: '#d97706', borderColor: '#fde68a', background: '#fffbeb' }}>
                                <Loader2 size={12} className="spin" />
                                任务排队中...
                              </span>
                            )}
                            {isFailed && (
                              <span className="pill pill-default" style={{ fontSize: 11, color: '#dc2626', borderColor: '#fecaca', background: '#fef2f2' }} title={doc.error_msg || ''}>
                                <AlertCircle size={12} />
                                解析失败
                              </span>
                            )}
                            {isIndexMissing && (
                              <span className="pill pill-default" style={{ fontSize: 11, color: '#dc2626', borderColor: '#fecaca', background: '#fef2f2' }}>
                                <AlertCircle size={12} />
                                索引缺失，当前不可检索
                              </span>
                            )}
                            {isIndexUnknown && !isRunning && !isPending && !isFailed && (
                              <span className="pill pill-default" style={{ fontSize: 11, color: '#64748b', borderColor: '#cbd5e1', background: '#f8fafc' }}>
                                <AlertCircle size={12} />
                                索引状态未知
                              </span>
                            )}

                            {/* DELETE BUTTON */}
                            {profile?.role === 'READONLY' ? (
                              <span 
                                className="btn-ghost" 
                                style={{ padding: 6, color: '#cbd5e1', cursor: 'not-allowed', display: 'flex', alignItems: 'center' }} 
                                title="只读访客受 RBAC 保护禁止删除文档"
                              >
                                <Lock size={14} />
                              </span>
                            ) : (
                              <>
                                {(isIndexMissing || isFailed) && (
                                  <button
                                    onClick={() => handleReingestDoc(doc.id, doc.filename)}
                                    className="btn-ghost"
                                    style={{ padding: 6, color: '#d97706' }}
                                    title="重新解析并构建索引"
                                  >
                                    <RefreshCw size={14} />
                                  </button>
                                )}
                                <button
                                  onClick={() => handleDeleteDoc(doc.id, doc.filename)}
                                  className="btn-ghost"
                                  style={{ padding: 6, color: '#94a3b8' }}
                                  title="删除文档与关联数据"
                                >
                                  <Trash2 size={14} />
                                </button>
                              </>
                            )}
                          </div>
                        </div>

                        {/* PROGRESS BAR (IF RUNNING/PENDING) */}
                        {(isRunning || isPending) && (
                          <div style={{ width: '100%', marginTop: 2 }}>
                            <div style={{ height: 4, width: '100%', background: '#e2e8f0', borderRadius: 999, overflow: 'hidden' }}>
                              <div 
                                style={{ 
                                  height: '100%', 
                                  width: isPending ? '20%' : `${Math.max(doc.task_progress || 10, 10)}%`, 
                                  background: 'var(--accent-primary)', 
                                  borderRadius: 999,
                                  transition: 'width 0.4s ease'
                                }} 
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}

        {/* TAB 3: MINIMALIST LIGHT CONFIG & PROMPT VIEW */}
        {activeTab === 'config' && (
          <div style={{ flex: 1, overflowY: 'auto', padding: 28, maxWidth: 680, width: '100%', margin: '0 auto' }}>
            <div className="card-clean" style={{ padding: 26 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                <div>
                  <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-primary)' }}>运行时配置与 Prompt 隔离</div>
                  <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>支持原子版本化热更新与快照隔离</div>
                </div>
                {tenantConfig && (
                  <span className="pill pill-accent">Version {tenantConfig.version_id}</span>
                )}
              </div>

              {profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN' && (
                <div style={{ padding: '8px 12px', borderRadius: 6, background: '#fffbeb', border: '1px solid #fde68a', color: '#b45309', fontSize: 12, marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Lock size={13} />
                  <span>当前角色为「{profile?.role}」，参数仅供只读预览，只有租户管理员可发布热更新。</span>
                </div>
              )}

              {configSuccess && (
                <div style={{ padding: '8px 12px', borderRadius: 6, background: '#ecfdf5', border: '1px solid #d1fae5', color: '#059669', fontSize: 12, marginBottom: 16 }}>
                  {configSuccess}
                </div>
              )}

              <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <label style={{ fontSize: 12, color: 'var(--text-secondary)' }}>大模型生成温度 (Temperature)</label>
                    <span style={{ fontSize: 12, fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--accent-primary)' }}>{tempTemperature}</span>
                  </div>
                  <input 
                    type="range" 
                    min="0.0" 
                    max="2.0" 
                    step="0.05"
                    value={tempTemperature} 
                    onChange={(e) => setTempTemperature(parseFloat(e.target.value))} 
                    disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'}
                    style={{ width: '100%', accentColor: 'var(--accent-primary)' }}
                  />
                </div>

                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
                    <label style={{ fontSize: 12, color: 'var(--text-secondary)' }}>检索 Top-K 召回数量</label>
                    <span style={{ fontSize: 12, fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--accent-primary)' }}>{tempTopK}</span>
                  </div>
                  <input 
                    type="range" 
                    min="1" 
                    max="20" 
                    step="1" 
                    value={tempTopK} 
                    onChange={(e) => setTempTopK(parseInt(e.target.value))} 
                    disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'}
                    style={{ width: '100%', accentColor: 'var(--accent-primary)' }}
                  />
                </div>

                <div>
                  <label style={{ display: 'block', fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>
                    租户专属人设 (System Persona)
                  </label>
                  <textarea 
                    className="input-minimal" 
                    rows={4} 
                    placeholder="输入企业专属系统人设与回答规范..."
                    value={tempPersona}
                    onChange={(e) => setTempPersona(e.target.value)}
                    disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'}
                  />
                </div>

                <button 
                  onClick={handleUpdateConfig} 
                  className="btn-solid" 
                  disabled={profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN'}
                  style={{ alignSelf: 'flex-start', marginTop: 4, opacity: (profile?.role !== 'TENANT_ADMIN' && profile?.role !== 'SYSTEM_ADMIN') ? 0.6 : 1 }}
                >
                  {(profile?.role === 'TENANT_ADMIN' || profile?.role === 'SYSTEM_ADMIN') ? '发布热更新' : '仅管理员可发布更新'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
