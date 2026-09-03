/**
 * Enterprise RAG API Client with JWT & SSE Streaming Support
 */

const API_BASE = '/api/v1';

export class ApiClient {
  static getToken(): string | null {
    return localStorage.getItem('rag_token');
  }

  static setToken(token: string) {
    localStorage.setItem('rag_token', token);
  }

  static setTokens(accessToken: string, refreshToken: string) {
    localStorage.setItem('rag_token', accessToken);
    localStorage.setItem('rag_refresh_token', refreshToken);
  }

  static clearToken() {
    localStorage.removeItem('rag_token');
    localStorage.removeItem('rag_refresh_token');
  }

  private static getHeaders(isFormData = false): Record<string, string> {
    const headers: Record<string, string> = {};
    if (!isFormData) {
      headers['Content-Type'] = 'application/json';
    }
    const token = this.getToken();
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
  }

  static async login(username: string, password: string, tenantName = 'default') {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, tenant_name: tenantName }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.message || 'Login failed');
    }
    const data = await res.json();
    this.setTokens(data.access_token, data.refresh_token);
    return data;
  }

  static async tryRefresh(): Promise<boolean> {
    const refreshToken = localStorage.getItem('rag_refresh_token');
    if (!refreshToken) return false;
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return false;
      const data = await res.json();
      this.setTokens(data.access_token, data.refresh_token);
      return true;
    } catch {
      return false;
    }
  }

  static async logout() {
    const token = this.getToken();
    if (token) {
      try {
        await fetch(`${API_BASE}/auth/logout`, {
          method: 'POST',
          headers: this.getHeaders(),
        });
      } catch {
        // Local credentials are still cleared if the API is unavailable.
      }
    }
    this.clearToken();
  }

  static async getProfile() {
    const res = await fetch(`${API_BASE}/auth/me`, {
      headers: this.getHeaders(),
    });
    if (!res.ok) throw new Error('Failed to fetch profile');
    return res.json();
  }

  static async getKnowledgeBases() {
    const res = await fetch(`${API_BASE}/knowledge-bases`, {
      headers: this.getHeaders(),
    });
    if (!res.ok) throw new Error('Failed to fetch knowledge bases');
    return res.json();
  }

  static async createKnowledgeBase(name: string, description?: string) {
    const res = await fetch(`${API_BASE}/knowledge-bases`, {
      method: 'POST',
      headers: this.getHeaders(),
      body: JSON.stringify({ name, description }),
    });
    if (!res.ok) throw new Error('Failed to create knowledge base');
    return res.json();
  }

  static async uploadDocument(kbId: string, file: File) {
    const formData = new FormData();
    formData.append('kb_id', kbId);
    formData.append('file', file);

    const res = await fetch(`${API_BASE}/documents/upload`, {
      method: 'POST',
      headers: this.getHeaders(true),
      body: formData,
    });
    if (!res.ok) {
      let errorMsg = `Upload failed (HTTP ${res.status})`;
      try {
        const err = await res.json();
        errorMsg = err.message || err.detail || errorMsg;
      } catch {
        if (res.status === 413) {
          errorMsg = '文件过大，超出了服务器上传限制(413)';
        } else if (res.status === 502 || res.status === 504) {
          errorMsg = '网关超时或后端服务不可用(502/504)';
        }
      }
      throw new Error(errorMsg);
    }
    return res.json();
  }

  static async getDocuments(kbId?: string) {
    const queryStr = kbId ? `?kb_id=${encodeURIComponent(kbId)}` : '';
    const res = await fetch(`${API_BASE}/documents${queryStr}`, {
      headers: this.getHeaders(),
    });
    if (!res.ok) throw new Error('Failed to fetch documents');
    return res.json();
  }

  static async deleteDocument(docId: string) {
    const res = await fetch(`${API_BASE}/documents/${docId}`, {
      method: 'DELETE',
      headers: this.getHeaders(),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.message || 'Delete failed');
    }
    return res.json();
  }

  static async reingestDocument(docId: string) {
    const res = await fetch(`${API_BASE}/documents/${docId}/reingest`, {
      method: 'POST',
      headers: this.getHeaders(),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.message || 'Re-ingestion failed');
    }
    return res.json();
  }

  static async getTenantConfig() {
    const res = await fetch(`${API_BASE}/tenants/config`, {
      headers: this.getHeaders(),
    });
    if (!res.ok) throw new Error('Failed to fetch tenant configuration');
    return res.json();
  }

  static async updateTenantConfig(config: any) {
    const res = await fetch(`${API_BASE}/tenants/config`, {
      method: 'PUT',
      headers: this.getHeaders(),
      body: JSON.stringify(config),
    });
    if (!res.ok) throw new Error('Failed to update tenant configuration');
    return res.json();
  }

  static async streamQueryRAG(
    kbId: string,
    query: string,
    mode: string,
    topK: number,
    onStatus: (msg: string) => void,
    onSources: (sources: any[], entities: any[]) => void,
    onToken: (delta: string) => void,
    onDone: (metrics: any) => void,
    onError: (err: string) => void,
    sessionId?: string,
  ) {
    try {
      const res = await fetch(`${API_BASE}/chat/query/stream`, {
        method: 'POST',
        headers: this.getHeaders(),
        body: JSON.stringify({
          kb_id: kbId,
          query,
          mode,
          top_k: topK || 8,
          stream: true,
          session_id: sessionId,
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.message || 'Streaming query failed');
      }

      const reader = res.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      if (!reader) throw new Error('No readable stream available');

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';

        for (const evt of events) {
          if (!evt.trim()) continue;
          const lines = evt.split('\n');
          let eventType = '';
          let dataStr = '';

          for (const line of lines) {
            if (line.startsWith('event: ')) {
              eventType = line.replace('event: ', '').trim();
            } else if (line.startsWith('data: ')) {
              dataStr = line.replace('data: ', '').trim();
            }
          }

          if (eventType && dataStr) {
            try {
              const parsed = JSON.parse(dataStr);
              if (eventType === 'status') onStatus(parsed.message || '');
              if (eventType === 'sources') onSources(parsed.sources || [], parsed.entities || []);
              if (eventType === 'token') onToken(parsed.delta || '');
              if (eventType === 'done') onDone(parsed);
              if (eventType === 'error') onError(parsed.message || 'Error');
            } catch (e) {
              console.error('Failed to parse SSE payload:', dataStr);
            }
          }
        }
      }
    } catch (error: any) {
      onError(error.message || 'Network error');
    }
  }
}
