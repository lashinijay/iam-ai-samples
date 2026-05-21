import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./index.css";
import { AsgardeoProvider } from "@asgardeo/react";
import config from "./config.js";

function ConfigurationError({ missingConfig }) {
  return (
    <div className="app-root">
      <h1>Missing configuration</h1>
      <p>The following values are required: {missingConfig.join(", ")}</p>
      <p>Set them as Vite environment variables before starting the app.</p>
    </div>
  );
}

const clientId = config.clientId;
const baseUrl = config.baseUrl;

const missingConfig = [];
if (!baseUrl) {
  missingConfig.push("baseUrl");
}
if (!clientId) {
  missingConfig.push("clientId");
}

if (missingConfig.length > 0) {
  console.error("⚠️ Missing required configuration:", missingConfig.join(", "));
  console.error("Please configure these values as Vite environment variables.");
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    {missingConfig.length > 0 ? (
      <ConfigurationError missingConfig={missingConfig} />
    ) : (
      <AsgardeoProvider baseUrl={baseUrl} clientId={clientId} scopes="openid profile internal_login agent:customer_service:invoke">
        <App />
      </AsgardeoProvider>
    )}
  </StrictMode>
);