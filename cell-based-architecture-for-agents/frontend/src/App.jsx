import { useState } from 'react'
import Chat from './components/Chat.jsx'
import { SignedIn, SignedOut, SignInButton} from "@asgardeo/react";

function App() {
 

  return (
    <div className="app-root">
      <SignedIn>
        <Chat />
      </SignedIn>
      <SignedOut>
        <SignInButton/>
      </SignedOut>
    </div>
  )
}

export default App
