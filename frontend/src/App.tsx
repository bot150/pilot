import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react'

type Health = { status: string; database: string }
type Incident = { id: number; title: string; service: string; status: string }
type Investigation = { incident: Incident; recalled_memories: MemoryItem[]; learned_pattern: string | null; recommendation: string }
type DocumentInfo = { id: string; filename: string; media_type: string; size_bytes: number; memory_count: number; status: string; uploaded_at: string | null }
type MemoryItem = { id: string; type: string; text: string; context?: string; date?: string }
type Indicator = 'retained' | 'recalled' | 'reflected'
type ApiObject = Record<string, unknown>
type ChatMessage = {
  id: string
  role: 'user' | 'assistant'
  text: string
  indicators?: Indicator[]
  memories?: MemoryItem[]
  loading?: boolean
  loadingLabel?: string
  incident?: Incident
  investigation?: Investigation
  evidence?: string
  resolveOpen?: boolean
  outcomeDraft?: string
  resolutionOutcome?: string
  retained?: boolean
}
type Conversation = { id: string; title: string; updatedAt: number; messages: ChatMessage[] }

const STORAGE_KEY = 'incidentpilot.conversations.v1'
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024
const examples = [
  { title: 'GATE Preparation', prompt: "I'm preparing for GATE 2027 and I'm learning Python." },
  { title: 'Ecommerce Project', prompt: "I'm working on an ecommerce project. Tell me some ideas." },
  { title: 'Python Learning', prompt: 'I am learning Python and want to build a steady practice habit.' },
  { title: 'Payment Service Incident', prompt: 'The payment API is failing after a deployment. What should I investigate first?' },
]

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  return body as T
}

function object(value: unknown): ApiObject | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as ApiObject : null
}

function firstString(record: ApiObject, keys: string[]): string | undefined {
  for (const key of keys) if (typeof record[key] === 'string' && record[key]) return record[key] as string
  return undefined
}

function normalizeMemories(value: unknown): MemoryItem[] {
  if (Array.isArray(value)) return value.flatMap((item, index) => normalizeOne(item, `${index}`))
  const record = object(value)
  if (!record) return []
  for (const key of ['results', 'memories', 'items', 'facts', 'episodes']) {
    if (Array.isArray(record[key])) return normalizeMemories(record[key])
    const nested = object(record[key])
    if (nested) {
      const found = normalizeMemories(nested)
      if (found.length) return found
    }
  }
  return normalizeOne(record, 'memory')
}

function normalizeOne(value: unknown, id: string): MemoryItem[] {
  if (typeof value === 'string' && value.trim()) return [{ id, type: 'Memory', text: value.trim() }]
  const record = object(value)
  if (!record) return []
  const text = firstString(record, ['text', 'content', 'summary', 'description', 'fact', 'memory', 'name'])
  if (!text) return []
  return [{
    id: firstString(record, ['id', 'memory_id']) || id,
    type: firstString(record, ['type', 'memory_type', 'category']) || 'Memory',
    text,
    context: firstString(record, ['context', 'source', 'document_id']),
    date: firstString(record, ['timestamp', 'created_at', 'date']),
  }]
}

function newId(): string { return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}` }

function uploadDocumentRequest(file: File, onProgress: (percent: number) => void, onUploaded: () => void): Promise<{ document: DocumentInfo }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    const form = new FormData()
    form.append('file', file)
    request.open('POST', '/api/documents/upload')
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100))
    }
    request.upload.onload = onUploaded
    request.onload = () => {
      try {
        const body = JSON.parse(request.responseText || '{}')
        if (request.status >= 200 && request.status < 300) resolve(body)
        else reject(new Error(`Upload failed (${request.status})`))
      } catch {
        reject(new Error('Upload failed'))
      }
    }
    request.onerror = () => reject(new Error('Upload failed'))
    request.onabort = () => reject(new Error('Upload cancelled'))
    request.send(form)
  })
}

function memoryDocumentSource(context?: string): string | null {
  if (!context) return null
  return context.match(/^Uploaded document:\s*(.+?)\s+\(part \d+ of \d+\)$/)?.[1] || null
}

function formatFileSize(bytes: number): string {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function MemoryGlyph({ kind }: { kind: Indicator }) {
  const labels: Record<Indicator, string> = {
    retained: '✦ MEMORY CAPTURED',
    recalled: '↗ CONTEXT RECALLED',
    reflected: '✧ REFLECTED INSIGHT',
  }
  return <span className={`memory-badge ${kind}`}>{labels[kind]}</span>
}

function isResolvedIncidentMemory(memory: MemoryItem): boolean {
  return /resolved production incident|incident\s+\d+.{0,240}\bresolved\b|\bresolved\b.{0,240}\bincident\s+\d+/i.test(`${memory.text} ${memory.context || ''}`)
}

function PreviousIncidentMemory({ memories }: { memories: MemoryItem[] }) {
  const memory = memories.find(isResolvedIncidentMemory)
  if (!memory) return null
  return <aside className="learning-context-banner">
    <span className="learning-context-icon">↗</span>
    <div><strong>PREVIOUS INCIDENT RECALLED FROM HINDSIGHT</strong><p>{memory.text}</p><small>{memory.context || memory.type}</small></div>
  </aside>
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [incidentFormOpen, setIncidentFormOpen] = useState(false)
  const [incidentTitle, setIncidentTitle] = useState('Payment API latency spike')
  const [incidentService, setIncidentService] = useState('payments-api')
  const [incidentEvidence, setIncidentEvidence] = useState('p95 latency increased after the latest deployment.')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [memoryOpen, setMemoryOpen] = useState(true)
  const [recentOpen, setRecentOpen] = useState(true)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [uploadProcessing, setUploadProcessing] = useState(false)
  const [uploadNotice, setUploadNotice] = useState<DocumentInfo | null>(null)
  const [documents, setDocuments] = useState<DocumentInfo[]>([])
  const [selectedDocument, setSelectedDocument] = useState<DocumentInfo | null>(null)
  const [documentMemories, setDocumentMemories] = useState<MemoryItem[]>([])
  const [documentLoading, setDocumentLoading] = useState(false)
  const [documentError, setDocumentError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    api<Health>('/api/health').then(setHealth).catch(() => setHealth(null))
    api<DocumentInfo[]>('/api/documents').then(setDocuments).catch(() => setDocuments([]))
    try {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) {
        const parsed = JSON.parse(saved) as Conversation[]
        if (Array.isArray(parsed)) setConversations(parsed)
      }
    } catch { /* Ignore unavailable or invalid local chat history. */ }
    setLoaded(true)
  }, [])

  useEffect(() => {
    if (loaded) localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations))
  }, [conversations, loaded])

  const activeConversation = conversations.find((chat) => chat.id === activeId)
  const currentMemories = [...(activeConversation?.messages || [])].reverse().find((message) => message.role === 'assistant' && message.memories?.length)?.memories || []

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [activeConversation?.messages, busy])

  function updateConversation(id: string, update: (chat: Conversation) => Conversation) {
    setConversations((current) => current.map((chat) => chat.id === id ? update(chat) : chat))
  }

  function newChat() {
    const chat: Conversation = { id: newId(), title: 'New conversation', updatedAt: Date.now(), messages: [] }
    setConversations((current) => [chat, ...current])
    setActiveId(chat.id)
    setDraft('')
    setError('')
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  function startExample(prompt: string) {
    const chat: Conversation = { id: newId(), title: prompt.slice(0, 36), updatedAt: Date.now(), messages: [] }
    setConversations((current) => [chat, ...current])
    setActiveId(chat.id)
    setDraft(prompt)
    setError('')
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  async function sendMessage(event?: FormEvent) {
    event?.preventDefault()
    const text = draft.trim()
    if (!text || busy) return

    let chat = activeConversation
    if (!chat) {
      chat = { id: newId(), title: text.slice(0, 36), updatedAt: Date.now(), messages: [] }
      setConversations((current) => [chat!, ...current])
      setActiveId(chat.id)
    }
    const chatId = chat.id
    const title = chat.messages.length ? chat.title : text.length > 36 ? `${text.slice(0, 36)}…` : text
    const userMessage: ChatMessage = { id: newId(), role: 'user', text }
    const assistantId = newId()
    const pending: ChatMessage = { id: assistantId, role: 'assistant', text: '', loading: true }
    updateConversation(chatId, (current) => ({
      ...current, title, updatedAt: Date.now(), messages: [...current.messages, userMessage, pending],
    }))
    if (!activeConversation) {
      setConversations((current) => current.map((item) => item.id === chatId ? { ...item, title, messages: [userMessage, pending] } : item))
    }
    setDraft('')
    setBusy(true)
    setError('')

    const patchAssistant = (patch: Partial<ChatMessage>) => updateConversation(chatId, (current) => ({
      ...current,
      updatedAt: Date.now(),
      messages: current.messages.map((message) => message.id === assistantId ? { ...message, ...patch, loading: false } : message),
    }))

    try {
      const history = chat.messages
        .filter((item) => !item.loading && item.text.trim())
        .slice(-12)
        .map((item) => ({ role: item.role, content: item.text }))
      const result = await api<{
        answer: string
        memories: MemoryItem[]
        reflected: boolean
        retained: boolean
        retained_text?: string | null
        recall_warning?: string | null
        retain_warning?: string | null
      }>('/api/chat', {
        method: 'POST',
        body: JSON.stringify({ message: text, history }),
      })
      const memories = normalizeMemories(result.memories)
      const retainedMemory: MemoryItem[] = result.retained && result.retained_text
        ? [{ id: userMessage.id, type: 'Memory', text: result.retained_text, context: title, date: 'Stored just now' }]
        : []
      patchAssistant({
        text: result.answer,
        indicators: [...(memories.length ? ['recalled' as const] : []), ...(result.reflected ? ['reflected' as const] : []), ...(result.retained ? ['retained' as const] : [])],
        memories: [...retainedMemory, ...memories],
      })
      if (result.recall_warning || result.retain_warning) setError('Your answer is ready, but memory could not be fully updated. You can continue the conversation.')
    } catch {
      updateConversation(chatId, (current) => ({
        ...current,
        messages: current.messages.filter((message) => message.id !== assistantId),
      }))
      setError('Something went wrong. Try again.')
    } finally {
      setBusy(false)
    }
  }

  async function investigateIncident(event?: FormEvent) {
    event?.preventDefault()
    if (busy) return
    const title = incidentTitle.trim()
    const service = incidentService.trim()
    const evidence = incidentEvidence.trim()
    if (!title || !service) {
      setError('Enter an incident title and service to investigate.')
      return
    }
    await createAndInvestigateIncident(title, service, evidence)
  }

  async function createAndInvestigateIncident(title: string, service: string, evidence: string) {
    if (busy) return
    let chat = activeConversation
    if (!chat) {
      chat = { id: newId(), title: title.slice(0, 36), updatedAt: Date.now(), messages: [] }
      setConversations((current) => [chat!, ...current])
      setActiveId(chat.id)
    }
    const chatId = chat.id
    const userMessage: ChatMessage = { id: newId(), role: 'user', text: `${title}\nService: ${service}${evidence ? `\nEvidence: ${evidence}` : ''}` }
    const assistantId = newId()
    const pending: ChatMessage = { id: assistantId, role: 'assistant', text: '', loading: true, loadingLabel: 'Creating incident and searching Hindsight memory' }
    updateConversation(chatId, (current) => ({
      ...current, title: current.messages.length ? current.title : title, updatedAt: Date.now(), messages: [...current.messages, userMessage, pending],
    }))
    if (!activeConversation) {
      setConversations((current) => current.map((item) => item.id === chatId ? { ...item, title: title.slice(0, 36), messages: [userMessage, pending] } : item))
    }
    setBusy(true)
    setError('')
    setIncidentFormOpen(false)
    let created: Incident | null = null
    try {
      created = await api<Incident>('/api/incidents', {
        method: 'POST', body: JSON.stringify({ title, service }),
      })
      const result = await api<Investigation>(`/api/incidents/${created.id}/investigate`, {
        method: 'POST', body: JSON.stringify({ evidence }),
      })
      const investigation = { ...result, recalled_memories: normalizeMemories(result.recalled_memories) }
      updateConversation(chatId, (current) => ({
        ...current,
        updatedAt: Date.now(),
        messages: current.messages.map((message) => message.id === assistantId
          ? { ...message, loading: false, text: 'Investigation complete.', incident: result.incident, investigation, evidence, memories: investigation.recalled_memories, indicators: investigation.recalled_memories.length ? ['recalled'] : [] }
          : message),
      }))
    } catch {
      const message = created
        ? 'The incident was created, but the investigation could not be completed. Check that the backend and Hindsight are available, then try again.'
        : 'The backend is unavailable, so the incident could not be created. Check that the backend is running and try again.'
      updateConversation(chatId, (current) => ({
        ...current,
        messages: current.messages.map((item) => item.id === assistantId ? { ...item, loading: false, text: message, incident: created || undefined } : item),
      }))
      setError('Incident investigation could not be completed.')
    } finally {
      setBusy(false)
    }
  }

  function createSimilarIncident() {
    const title = 'Payment API latency spike again'
    const service = 'payments-api'
    const evidence = 'p95 latency increased again after a recent deployment.'
    setIncidentTitle(title)
    setIncidentService(service)
    setIncidentEvidence(evidence)
    void createAndInvestigateIncident(title, service, evidence)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      void sendMessage()
    }
  }

  async function submitDocument() {
    if (!selectedFile || uploading || busy) return
    setUploading(true)
    setUploadProgress(0)
    setUploadProcessing(false)
    setError('')
    try {
      const result = await uploadDocumentRequest(
        selectedFile,
        setUploadProgress,
        () => setUploadProcessing(true),
      )
      setDocuments((current) => [result.document, ...current.filter((item) => item.id !== result.document.id)])
      setUploadNotice(result.document)
      setSelectedFile(null)
      setDocumentError('')
    } catch {
      setError('This document could not be added to memory. Check the file and try again.')
    } finally {
      setUploading(false)
      setUploadProcessing(false)
    }
  }

  async function openDocument(document: DocumentInfo) {
    setSelectedDocument(document)
    setDocumentMemories([])
    setDocumentError('')
    setDocumentLoading(true)
    try {
      const result = await api<{ document: DocumentInfo; memories: MemoryItem[] }>(`/api/documents/${encodeURIComponent(document.id)}/memories`)
      setSelectedDocument(result.document)
      setDocumentMemories(normalizeMemories(result.memories))
    } catch {
      setDocumentError('Could not load related memories. Try again.')
    } finally {
      setDocumentLoading(false)
    }
  }

  function toggleResolveForm(chatId: string, messageId: string) {
    updateConversation(chatId, (chat) => ({
      ...chat,
      messages: chat.messages.map((message) => message.id === messageId ? { ...message, resolveOpen: !message.resolveOpen } : message),
    }))
  }

  function updateOutcomeDraft(chatId: string, messageId: string, outcomeDraft: string) {
    updateConversation(chatId, (chat) => ({
      ...chat,
      messages: chat.messages.map((message) => message.id === messageId ? { ...message, outcomeDraft } : message),
    }))
  }

  async function resolveIncident(chatId: string, messageId: string, incident: Incident, evidence: string, outcome: string) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const result = await api<{ success: boolean; incident: Incident; retained: boolean; outcome: string }>(`/api/incidents/${incident.id}/resolve`, {
        method: 'POST', body: JSON.stringify({ outcome, evidence }),
      })
      updateConversation(chatId, (chat) => ({
        ...chat,
        messages: chat.messages.map((message) => message.id === messageId ? {
          ...message,
          incident: result.incident,
          investigation: message.investigation ? { ...message.investigation, incident: result.incident } : undefined,
          resolutionOutcome: result.outcome,
          retained: result.retained,
          resolveOpen: false,
        } : message),
      }))
    } catch {
      setError('Something went wrong. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const sortedConversations = [...conversations].sort((a, b) => b.updatedAt - a.updatedAt)

  return <div className={`app-shell ${recentOpen ? '' : 'recent-collapsed'} ${memoryOpen ? '' : 'memory-collapsed'}`}>
    <aside className={`sidebar ${recentOpen ? '' : 'collapsed'}`}>
      <div className="brand-row">
        <button className="brand" onClick={newChat} aria-label="IncidentPilot home">
          <span className="brand-mark"><span>IP</span><i /></span>
          <span className="brand-copy"><strong>INCIDENTPILOT</strong><small>MEMORY ORBIT</small></span>
        </button>
        <button className="recent-toggle" onClick={() => setRecentOpen((value) => !value)} aria-label={recentOpen ? 'Hide Recent Chats' : 'Show Recent Chats'} title={recentOpen ? 'Hide Recent Chats' : 'Show Recent Chats'}>{recentOpen ? '‹' : '›'}</button>
      </div>

      <button className="new-chat" onClick={newChat}><span>＋</span><b>New Chat</b></button>

      <div className="sidebar-scroll">
        <div className="side-section">
          <div className="side-heading"><span>RECENT CHATS</span><span className="section-mark">◌</span></div>
          <nav className="conversation-list" aria-label="Recent conversations">
            {sortedConversations.slice(0, 8).map((chat, index) => <button key={chat.id} className={`conversation-link ${activeId === chat.id ? 'active' : ''}`} onClick={() => { setActiveId(chat.id); setError('') }} title={chat.title}>
              <span className={`chat-icon topic-${index % 4}`} /> <span className="conversation-title">{chat.title}</span>
            </button>)}
            {!sortedConversations.length && <div className="empty-recent">Your conversations will gather here.</div>}
          </nav>
        </div>

        <div className="side-section examples-section">
          <div className="side-heading"><span>START WITH A THREAD</span></div>
          {examples.map((example, index) => <button key={example.title} className="example-link" onClick={() => startExample(example.prompt)} title={example.title}>
            <span className={`example-dot dot-${index}`} /><span>{example.title}</span>
          </button>)}
        </div>
      </div>

      <div className="sidebar-bottom">
        {settingsOpen && <div className="settings-popover"><strong>Workspace settings</strong><span>Memory lives in your Hindsight bank.</span><span>Conversation history is saved in this browser.</span></div>}
        <button className={`settings-button ${settingsOpen ? 'selected' : ''}`} onClick={() => setSettingsOpen((value) => !value)}><span>⚙</span><b>Settings</b></button>
        <div className="profile-row"><div className="avatar">IP</div><div><strong>IncidentPilot</strong><small>Personal workspace</small></div><span className="more">···</span></div>
      </div>
    </aside>

    <main className="chat-layout">
      <header className="chat-header">
        <div className="mobile-brand"><span className="brand-mark"><span>IP</span><i /></span><strong>INCIDENTPILOT</strong></div>
        <div className="header-title"><span>{activeConversation?.title || 'Your memory workspace'}</span><small>Conversation space <i>·</i> Private by design</small></div>
        <div className="header-actions">
          <span className={`connection-pill ${health ? 'connected' : 'disconnected'}`}><i />MEMORY ACTIVE</span>
          <button className={`panel-toggle ${memoryOpen ? 'active' : ''}`} onClick={() => setMemoryOpen((value) => !value)} aria-label={memoryOpen ? 'Hide Memory Orbit' : 'Show Memory Orbit'} title={memoryOpen ? 'Hide Memory Orbit' : 'Show Memory Orbit'}>✧</button>
        </div>
      </header>

      <div className="chat-content">
        {!activeConversation?.messages.length ? <section className="welcome-state">
          <div className="welcome-orbit" aria-hidden="true"><span className="orbit-core">✦</span><i className="orbit-ring ring-one" /><i className="orbit-ring ring-two" /><b className="orbit-dot dot-a" /><b className="orbit-dot dot-b" /><b className="orbit-dot dot-c" /></div>
          <p className="welcome-kicker">INCIDENTPILOT <span>•</span> MEMORY ORBIT</p>
          <h1>An AI that remembers<br /><em>what matters.</em></h1>
          <p className="welcome-subtitle">Capture useful context. Recall it when you need it.<br />Build better conversations over time.</p>
          <div className="feature-row">
            <button className="feature-card remember" onClick={() => startExample(examples[0].prompt)}><span className="feature-icon">✦</span><strong>MEMORY</strong><small>Capture useful information.</small></button>
            <button className="feature-card recall" onClick={() => setDraft('What am I preparing for?')}><span className="feature-icon">↗</span><strong>RECALL</strong><small>Bring relevant context back.</small></button>
            <button className="feature-card reflect" onClick={() => setDraft('What patterns do you notice in what I have shared?')}><span className="feature-icon">✧</span><strong>REFLECT</strong><small>Build understanding from experience.</small></button>
          </div>
          {!conversations.length && <button className="try-prompt" onClick={() => startExample(examples[1].prompt)}>Start with the ecommerce demo <span>→</span></button>}
        </section> : <section className="message-list" aria-label="Conversation">
          {activeConversation.messages.map((message) => <article key={message.id} className={`message-row ${message.role}`}>
            {message.role === 'assistant' && <div className="assistant-avatar"><span>IP</span><i /></div>}
            <div className="message-column">
              <div className={`message-bubble ${message.role}`}>
                {message.loading ? <div className="thinking-state"><span className="thinking-orbit"><i /><b /></span><span>{message.loadingLabel || 'Thinking'}<span className="thinking-ellipsis">…</span></span></div> : <div className="message-text">{message.text}</div>}
              </div>
              {message.indicators?.length ? <div className="message-indicators">{message.indicators.map((kind) => <MemoryGlyph key={kind} kind={kind} />)}</div> : null}
              {message.investigation && <section className="investigation-result" aria-label="Incident investigation result">
                <div className="investigation-heading"><strong>{message.investigation.incident.title}</strong><span className={`incident-status ${(message.incident || message.investigation.incident).status}`}>Status: {(message.incident || message.investigation.incident).status}</span></div>
                <div className="investigation-service">{message.investigation.incident.service}</div>
                {(() => {
                  const incidentStatus = (message.incident || message.investigation!.incident).status
                  const learnedFromPrior = message.investigation!.recalled_memories.some(isResolvedIncidentMemory)
                  return <div className="incident-workflow" aria-label="Incident workflow progress">
                    <span className="workflow-done"><i>✓</i> Incident</span><span className="workflow-done"><i>✓</i> Investigate</span>
                    <span className={incidentStatus === 'resolved' ? 'workflow-done' : 'workflow-current'}><i>{incidentStatus === 'resolved' ? '✓' : '3'}</i> Resolve + Retain</span>
                    <span className={learnedFromPrior ? 'workflow-done' : 'workflow-waiting'}><i>{learnedFromPrior ? '✓' : '4'}</i> Learn</span>
                  </div>
                })()}
                <PreviousIncidentMemory memories={message.investigation.recalled_memories} />
                <div className="investigation-section"><span>HINDSIGHT RECALL</span>{message.investigation.recalled_memories.length ? <ul>{message.investigation.recalled_memories.map((memory) => <li key={memory.id}><b>{memory.type}</b>{memory.text}</li>)}</ul> : <p>No relevant historical memories were returned.</p>}</div>
                {message.investigation.learned_pattern && <div className="investigation-section"><span>LEARNED PATTERN · REFLECT</span><p>{message.investigation.learned_pattern}</p></div>}
                <div className="investigation-section recommendation"><span>{message.investigation.recalled_memories.some(isResolvedIncidentMemory) ? 'RECOMMENDATION · USING RECALLED EXPERIENCE' : 'OPERATIONAL RECOMMENDATION'}</span><p>{message.investigation.recommendation}</p></div>
                {(message.incident || message.investigation.incident).status === 'resolved' ? <div className="resolution-confirmation"><strong>✓ Outcome retained in Hindsight</strong><p>{message.resolutionOutcome}</p><button className="similar-incident-button" disabled={busy} onClick={createSimilarIncident}>Create Similar Incident <span>→</span></button></div> : <div className="resolve-area">
                  {!message.resolveOpen ? <button className="resolve-chat-button" disabled={busy} onClick={() => toggleResolveForm(activeConversation.id, message.id)}>Resolve Incident</button> : <form className="resolution-form" onSubmit={(event) => { event.preventDefault(); void resolveIncident(activeConversation.id, message.id, message.incident || message.investigation!.incident, message.evidence || '', message.outcomeDraft || '') }}>
                    <label htmlFor={`outcome-${message.id}`}>Resolution / outcome</label>
                    <textarea id={`outcome-${message.id}`} value={message.outcomeDraft || ''} onChange={(event) => updateOutcomeDraft(activeConversation.id, message.id, event.target.value)} placeholder="Describe what resolved the incident…" minLength={3} maxLength={10000} rows={3} required />
                    <div className="resolution-actions"><button type="button" className="resolution-cancel" disabled={busy} onClick={() => toggleResolveForm(activeConversation.id, message.id)}>Cancel</button><button type="submit" className="resolve-chat-button" disabled={busy || (message.outcomeDraft || '').trim().length < 3}>{busy ? 'Retaining outcome…' : 'Resolve & retain outcome'}</button></div>
                  </form>}
                </div>}
              </section>}
              {message.indicators?.includes('recalled') && [...new Set((message.memories || []).map((memory) => memoryDocumentSource(memory.context)).filter((name): name is string => Boolean(name)))].map((filename) => <span className="document-source-tag" key={filename}>📄 {filename}</span>)}
            </div>
          </article>)}
          <div ref={endRef} />
        </section>}
      </div>

      <div className="composer-wrap">
        {incidentFormOpen && <form className="incident-entry-form" onSubmit={(event) => void investigateIncident(event)}>
          <div className="incident-form-heading"><strong>Investigate an incident</strong><button type="button" onClick={() => setIncidentFormOpen(false)} aria-label="Close incident form">×</button></div>
          <label>Title<input value={incidentTitle} onChange={(event) => setIncidentTitle(event.target.value)} maxLength={200} required /></label>
          <label>Service<input value={incidentService} onChange={(event) => setIncidentService(event.target.value)} maxLength={100} required /></label>
          <label>Evidence<textarea value={incidentEvidence} onChange={(event) => setIncidentEvidence(event.target.value)} maxLength={10000} rows={2} placeholder="What have you observed?" /></label>
          <button className="investigate-submit" type="submit" disabled={busy}>{busy ? 'Investigating…' : 'Create & investigate →'}</button>
        </form>}
        {uploadNotice && <article className="upload-success-card"><span className="upload-success-icon">▤</span><div><strong>📄 DOCUMENT REMEMBERED</strong><b>{uploadNotice.filename}</b><p>Important information from this document has been added to memory.</p></div><button onClick={() => setUploadNotice(null)} aria-label="Dismiss document remembered notice">×</button></article>}
        {error && <div className="chat-error" role="alert"><span>!</span>{error}</div>}
        {selectedFile && <div className="selected-file-card"><span className="file-type-icon">{selectedFile.name.toLowerCase().endsWith('.pdf') ? 'PDF' : selectedFile.name.split('.').pop()?.toUpperCase()}</span><div className="selected-file-info"><strong>{selectedFile.name}</strong><small>{formatFileSize(selectedFile.size)} · ready to add to memory</small>{uploading && <div className="upload-progress-track"><i style={{ width: `${uploadProcessing ? 100 : uploadProgress}%` }} /></div>}</div><button className="remove-file" type="button" disabled={uploading} onClick={() => setSelectedFile(null)} aria-label="Remove selected document">×</button>{uploading && <span className="upload-stage">{uploadProcessing ? 'Adding to memory…' : `${uploadProgress}%`}</span>}</div>}
        <form className="composer" onSubmit={(event) => void sendMessage(event)}>
          <textarea ref={inputRef} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={handleKeyDown} placeholder="Ask IncidentPilot anything..." rows={1} aria-label="Message IncidentPilot" />
          <div className="composer-footer"><label className={`upload-trigger ${uploading || busy ? 'disabled' : ''}`} title="Upload a PDF, TXT, DOCX, or CSV document"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 11.5 12 20a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5" /></svg><span>Upload Document</span><input type="file" accept=".pdf,.txt,.docx,.csv,application/pdf,text/plain,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/csv" disabled={uploading || busy} onChange={(event) => { const file = event.currentTarget.files?.[0]; if (file) { const suffix = file.name.split('.').pop()?.toLowerCase(); if (file.size > MAX_UPLOAD_BYTES) { setError('Documents must be 10 MB or smaller.'); setSelectedFile(null) } else if (!['pdf', 'txt', 'docx', 'csv'].includes(suffix || '')) { setError('Choose a PDF, TXT, DOCX, or CSV document.'); setSelectedFile(null) } else { setSelectedFile(file); setError('') } } event.currentTarget.value = '' }} /></label><button className="investigate-trigger" type="button" disabled={busy || uploading} onClick={() => setIncidentFormOpen((value) => !value)}>＋ Investigate incident</button><span className="composer-hint"><kbd>↵</kbd> send <span className="hint-divider">·</span> <kbd>⇧ ↵</kbd> new line</span><div className="composer-actions">{selectedFile && <button className="remember-file-button" type="button" onClick={() => void submitDocument()} disabled={uploading || busy}>{uploading ? <span className="send-spinner" /> : 'Add to memory'}</button>}<button className="send-button" type="submit" disabled={busy || uploading || !draft.trim()} aria-label="Send message">{busy || uploading ? <span className="send-spinner" /> : <span>➜</span>}</button></div></div>
        </form>
        <p className="composer-note">Your useful context stays connected to your conversation.</p>
      </div>
    </main>

    <aside className={`memory-panel ${memoryOpen ? '' : 'closed'}`} aria-label="Memory Orbit">
      <div className="memory-panel-header"><div><span className="panel-eyebrow">MEMORY ORBIT</span><h2>Current memory</h2></div><span className="memory-count">{currentMemories.length.toString().padStart(2, '0')}</span></div>
      <div className="orbit-status"><span className="orbit-pulse" />Context connected to this conversation</div>
      <div className="memory-caption"><span>ACTIVE CONTEXT</span><i /></div>
      {currentMemories.length ? <div className="memory-list">{currentMemories.map((memory, index) => <article className="memory-card" key={memory.id}>
        <span className={`memory-node node-${index % 3}`} />
        <div className="memory-card-content"><div className="memory-card-top"><span className="memory-card-icon">✦</span><span className="memory-type">{memory.type}</span></div>
          <p>{memory.text}</p>
          {(memory.context || memory.date) && <div className="memory-meta">{memory.context}{memory.context && memory.date ? ' · ' : ''}{memory.date}</div>}
        </div>
      </article>)}</div> : <div className="memory-empty"><div className="empty-orbit"><i /><b /><span>✦</span></div><strong>No active context yet</strong><span>Memories returned for this conversation will orbit here.</span></div>}
      <div className="documents-section">
        <div className="documents-heading"><span>DOCUMENTS</span><span>{documents.filter((document) => document.status === 'ready').length.toString().padStart(2, '0')}</span></div>
        {selectedDocument ? <div className="document-detail">
          <button className="document-back" onClick={() => { setSelectedDocument(null); setDocumentMemories([]); setDocumentError('') }}>← All documents</button>
          <div className="document-detail-card"><span className="document-file-icon">▤</span><strong>{selectedDocument.filename}</strong><span>{formatFileSize(selectedDocument.size_bytes)} · {selectedDocument.status}</span><span>{selectedDocument.memory_count} memory {selectedDocument.memory_count === 1 ? 'item' : 'items'}</span></div>
          <div className="document-memories-label">RELATED MEMORIES</div>
          {documentLoading ? <div className="document-load-state"><span className="send-spinner" /> Searching document memory…</div> : documentError ? <p className="document-empty">{documentError}</p> : documentMemories.length ? <div className="document-memory-list">{documentMemories.map((memory) => <article key={memory.id} className="document-memory-card"><span>✦</span><p>{memory.text}</p>{memory.date && <small>{memory.date}</small>}</article>)}</div> : <p className="document-empty">No related memories were returned for this document.</p>}
        </div> : documents.length ? <div className="document-list">{documents.filter((document) => document.status === 'ready').map((document) => <button className="document-list-item" key={document.id} onClick={() => void openDocument(document)}><span className="document-file-icon">▤</span><span className="document-row-copy"><strong>{document.filename}</strong><small>{document.memory_count} {document.memory_count === 1 ? 'memory' : 'memories'}</small></span><span className="document-chevron">›</span></button>)}</div> : <p className="documents-empty">Documents you add to memory will appear here.</p>}
      </div>
      <div className="memory-panel-footer"><span className="privacy-icon">◈</span><span>Connected to your Hindsight memory bank</span></div>
    </aside>
  </div>
}
