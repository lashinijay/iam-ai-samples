// Default endpoint configuration for the chat API.
// The user can override this at runtime via the settings panel in the chat UI.
// The endpoint should accept POST requests with JSON body: { message: string }
// Response can be: { response }, { message }, { text }, { content }, or { reply }
// It may also return a plain text response.

const config = {
  defaultEndpointUrl: import.meta.env.VITE_CHAT_ENDPOINT_URL || '',
  clientId: import.meta.env.VITE_REACT_APP_CLIENT_ID,
  baseUrl: import.meta.env.VITE_ASGARDEO_BASE_URL,
};

export default config
