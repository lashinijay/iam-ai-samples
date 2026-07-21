import ReactMarkdown from 'react-markdown'

function formatTime(date) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/**
 * MessageBubble
 * Props:
 *   message  – { id, role: 'user'|'bot', text, timestamp: Date }
 */
export default function MessageBubble({ message }) {
  const isUser = message.role === 'user'

  return (
    <div className={`message-row ${message.role}`}>
      <span className="message-meta">
        {isUser ? 'You' : 'AI Agent'}
      </span>
      <div className="message-bubble" role="article" aria-label={`${isUser ? 'Your' : 'AI Agent'} message`}>
        {isUser ? message.text : <ReactMarkdown>{message.text}</ReactMarkdown>}
      </div>
      <span className="message-time">{formatTime(message.timestamp)}</span>
    </div>
  )
}
