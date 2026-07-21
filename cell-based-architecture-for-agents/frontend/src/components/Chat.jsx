import { useState, useRef, useEffect, useCallback } from 'react'
import { useAsgardeo } from '@asgardeo/react'
import MessageBubble from './MessageBubble.jsx'
import config from '../config.js'

function BotIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="3" y="8" width="18" height="13" rx="2" />
      <path d="M9 8V6a3 3 0 0 1 6 0v2" />
      <circle cx="9" cy="14" r="1.5" />
      <circle cx="15" cy="14" r="1.5" />
      <path d="M9.5 18h5" />
    </svg>
  )
}

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

function ChatBubbleIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

let nextId = 1

const ENDPOINT_URL = config.defaultEndpointUrl

function extractText(data) {
  if (typeof data === 'string') return data
  return (
    data?.response ??
    data?.message ??
    data?.text ??
    data?.content ??
    data?.reply ??
    data?.answer ??
    JSON.stringify(data)
  )
}




export default function Chat() {
  const { user, isSignedIn, getAccessToken, signOut, signIn } = useAsgardeo()

  const [messages,      setMessages]      = useState([])
  const [inputText,     setInputText]     = useState('')
  const [isTyping,      setIsTyping]      = useState(false)
  const [endpointError, setEndpointError] = useState('')
  const [consentPending, setConsentPending] = useState(false)
  const [sessionId,     setSessionId]     = useState(null)

  const messagesEndRef  = useRef(null)
  const textareaRef     = useRef(null)
  // Stores pending {text, sessionId} for auto-retry after consent popup.
  const pendingRetryRef = useRef(null)
  // Handle to the consent popup; used to validate postMessage sender.
  const consentPopupRef = useRef(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isTyping])

  const handleInputChange = (e) => {
    setInputText(e.target.value)
    const ta = textareaRef.current
    if (ta) {
      ta.style.height = 'auto'
      ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'
    }
  }

  // Core send logic — separated so auto-retry can call it directly.
  const doSend = useCallback(async (text, sessionOverride) => {
    const activeSession = sessionOverride ?? sessionId

    setIsTyping(true)
    setEndpointError('')

    try {
      if (!isSignedIn) {
        await signIn()
        return
      }

      const accessToken = await getAccessToken()

      const headers = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${accessToken}`,
      }

      const payload = { message: text }
      if (activeSession) payload.session_id = activeSession

      const res = await fetch(ENDPOINT_URL, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      })

      if (res.status === 401) {
        const data = await res.json()

        if (data.session_id) setSessionId(data.session_id)

        const needsConsent =
          data.error === 'consent_required' ||
          data.error === 'step_up_authentication_required'

        if (needsConsent && data.consent_url) {
          // Save pending request for auto-retry after consent.
          pendingRetryRef.current = { text, sessionId: data.session_id || activeSession }
          setConsentPending(true)
          consentPopupRef.current = window.open(data.consent_url, 'agent_consent', 'width=520,height=640')

          const promptText =
            data.error === 'step_up_authentication_required'
              ? `Additional authorization is required for **${data.tool || 'this action'}**. Please approve in the popup — your request will continue automatically.`
              : 'The agent needs your permission to access the required scopes. Please approve in the popup — your request will continue automatically.'

          setMessages((prev) => [...prev, {
            id: nextId++, role: 'bot', text: promptText, timestamp: new Date(),
          }])
          return
        }

        throw new Error(`Server returned ${res.status} ${res.statusText}`)
      }

      if (!res.ok) throw new Error(`Server returned ${res.status} ${res.statusText}`)

      setConsentPending(false)

      const contentType = res.headers.get('content-type') ?? ''
      const data = contentType.includes('application/json')
        ? await res.json()
        : await res.text()

      if (data.session_id) setSessionId(data.session_id)

      setMessages((prev) => [...prev, {
        id: nextId++, role: 'bot', text: extractText(data), timestamp: new Date(),
      }])
    } catch (err) {
      setMessages((prev) => [...prev, {
        id: nextId++, role: 'bot', text: `Error: ${err.message}`, isError: true, timestamp: new Date(),
      }])
      setEndpointError(err.message)
    } finally {
      setIsTyping(false)
    }
  }, [isSignedIn, getAccessToken, signIn, sessionId])

  // Listen for postMessage from auth-service callback; auto-retry pending request.
  useEffect(() => {
    const handleConsentComplete = (event) => {
      // Only accept messages from the popup we opened — defends against
      // unrelated windows/iframes spoofing the resume trigger.
      if (!consentPopupRef.current || event.source !== consentPopupRef.current) return
      if (event.data?.type !== 'agent_consent_complete') return
      const pending = pendingRetryRef.current
      if (!pending) return
      pendingRetryRef.current = null
      consentPopupRef.current = null
      setConsentPending(false)
      setMessages((prev) => [...prev, {
        id: nextId++, role: 'bot', text: 'Authorization complete — continuing your request…', timestamp: new Date(),
      }])
      doSend(pending.text, pending.sessionId)
    }
    window.addEventListener('message', handleConsentComplete)
    return () => window.removeEventListener('message', handleConsentComplete)
  }, [doSend])

  const sendMessage = useCallback(async () => {
    const text = inputText.trim()
    if (!text || isTyping) return

    setMessages((prev) => [...prev, { id: nextId++, role: 'user', text, timestamp: new Date() }])
    setInputText('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'

    await doSend(text, null)
  }, [inputText, isTyping, doSend])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const rawUsername = user?.username || user?.userName || 'User'
  const username = String(rawUsername)
  const initials = username.charAt(0).toUpperCase()
  const canSend  = inputText.trim().length > 0 && !isTyping

  return (
    <div className="chat-page">
      <header className="chat-header">
        <div className="chat-header-brand">
          <div className="chat-header-avatar">
            <BotIcon />
          </div>
          <div>
            <div className="chat-header-title">AI Agent</div>
            <div className="chat-header-subtitle">{isTyping ? 'Typing…' : 'Online'}</div>
          </div>
        </div>

        <div className="chat-header-right">
          <div className="user-chip" aria-label={`Logged in as ${username}`}>
            <span className="user-chip-icon">{initials}</span>
            <span className="user-chip-name">{username}</span>
          </div>
          <button
            className="btn btn-danger-ghost"
            onClick={() => signOut()}
            aria-label="Logout"
          >
            Logout
          </button>
        </div>
      </header>

      {endpointError && (
        <div className="error-banner">
          Last request failed: {endpointError}{' '}
          <button className="inline-link" onClick={() => setEndpointError('')}>Dismiss</button>
        </div>
      )}

      {consentPending && (
        <div className="error-banner" style={{ background: '#fff3cd', color: '#856404', borderColor: '#ffc107' }}>
          Waiting for authorization — please approve in the popup window.{' '}
          <button className="inline-link" onClick={() => setConsentPending(false)}>Dismiss</button>
        </div>
      )}

      <main className="chat-messages" aria-label="Chat messages" aria-live="polite">
        {messages.length === 0 && !isTyping ? (
          <div className="empty-state">
            <div className="empty-state-icon"><ChatBubbleIcon /></div>
            <h3>Start the conversation</h3>
            <p>Type a message below and the AI Agent will respond.</p>
          </div>
        ) : (
          <>
            {messages.map((msg) => (
              <MessageBubble key={msg.id} message={msg} />
            ))}

            {isTyping && (
              <div className="typing-indicator" aria-label="AI Agent is typing">
                <div className="typing-bubble">
                  <span className="typing-dot" />
                  <span className="typing-dot" />
                  <span className="typing-dot" />
                </div>
              </div>
            )}
          </>
        )}
        <div ref={messagesEndRef} />
      </main>

      <div className="chat-input-bar">
        <div className="chat-input-wrapper">
          <textarea
            ref={textareaRef}
            className="chat-input"
            placeholder="Type a message… (Enter to send, Shift+Enter for new line)"
            value={inputText}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            rows={1}
            aria-label="Message input"
            disabled={isTyping}
          />
        </div>
        <button
          className="send-btn"
          onClick={sendMessage}
          disabled={!canSend}
          aria-label="Send message"
          title="Send (Enter)"
        >
          <SendIcon />
        </button>
      </div>
    </div>
  )
}
