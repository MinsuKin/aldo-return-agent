import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { setSession, useSession } from "./api/session";
import { AdminPanel } from "./components/AdminPanel";
import { Banner } from "./components/Banner";
import { CasesView } from "./components/CasesView";
import { SignIn } from "./components/SignIn";
import { Workspace } from "./components/Workspace";
import { routeHref, useRoute, type Route } from "./hooks/useRoute";

const NAV: { route: Route; label: string }[] = [
  { route: "assistant", label: "Agents" },
  { route: "cases", label: "Cases" },
];

export default function App() {
  const session = useSession();
  const route = useRoute();
  const qc = useQueryClient();
  const admin = new URLSearchParams(window.location.search).get("admin") === "1";
  const [epoch, setEpoch] = useState(0);
  const [notice, setNotice] = useState<string | undefined>();
  const hadSession = useRef(false);
  const userSignedOut = useRef(false);

  // A session that disappears without the user asking (expired or rejected token) gets a notice.
  useEffect(() => {
    if (session) {
      hadSession.current = true;
      setNotice(undefined);
    } else if (hadSession.current && !userSignedOut.current) {
      setNotice("Your demo session ended. Sign in again to continue.");
      qc.clear();
    }
  }, [session, qc]);

  useEffect(() => {
    document.title = `${route === "cases" ? "Cases" : "Agents"} | Agentic Returns Gateway (prototype)`;
  }, [route]);

  function signOut() {
    userSignedOut.current = true;
    hadSession.current = false;
    setSession(null);
    qc.clear();
    window.setTimeout(() => {
      userSignedOut.current = false;
    }, 0);
  }

  const workspace = !!session && route === "assistant";

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <header className="site-header">
        <Banner />
        <div className="topbar">
          <h1>Agentic Returns Gateway</h1>
          <nav aria-label="Primary">
            <ul>
              {NAV.map((n) => (
                <li key={n.route}>
                  <a href={routeHref(n.route)} aria-current={route === n.route ? "page" : undefined}>
                    {n.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
          {session && (
            <div className="session" data-testid="session">
              <span>
                Demo sign-in, synthetic customer: <strong>{session.customer.display_name}</strong>
              </span>
              <button type="button" className="btn btn-small" onClick={signOut}>
                Sign out
              </button>
            </div>
          )}
        </div>
        {admin && (
          <AdminPanel
            onReset={() => {
              qc.clear();
              setEpoch((e) => e + 1);
            }}
          />
        )}
      </header>

      <main id="main" tabIndex={-1} className={workspace ? "main-workspace" : "main-page"}>
        {!session ? <SignIn notice={notice} /> : route === "cases" ? <CasesView /> : <Workspace key={epoch} />}
      </main>
    </>
  );
}
