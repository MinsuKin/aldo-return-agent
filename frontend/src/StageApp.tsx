import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { setSession, useSession } from "./api/session";
import { Banner } from "./components/Banner";
import { CasesView } from "./components/CasesView";
import { Stage } from "./components/Stage";
import { useRoute } from "./hooks/useRoute";

/**
 * The app shell: one small neutral strip, then either the stage (an iPhone-style chat with the personal agent
 * beside the live A2A trace) or the read-only Cases list.
 */
export default function StageApp() {
  const session = useSession();
  const route = useRoute();
  const qc = useQueryClient();
  const admin = new URLSearchParams(window.location.search).get("admin") === "1";
  const [notice, setNotice] = useState<string | undefined>();
  const hadSession = useRef(false);
  const userSignedOut = useRef(false);

  // A session that disappears without the owner asking (expired or rejected token) gets a notice.
  useEffect(() => {
    if (session) {
      hadSession.current = true;
      setNotice(undefined);
    } else if (hadSession.current && !userSignedOut.current) {
      setNotice("Your demo session ended. Pick a customer again to continue.");
      qc.clear();
    }
  }, [session, qc]);

  useEffect(() => {
    document.title = `${route === "cases" ? "Cases" : "Personal agent"} | Prototype`;
  }, [route]);

  function switchCustomer() {
    userSignedOut.current = true;
    hadSession.current = false;
    setSession(null);
    qc.clear();
    window.setTimeout(() => {
      userSignedOut.current = false;
    }, 0);
  }

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <Banner />
      <main id="main" tabIndex={-1} className="main-stage">
        <h1 className="sr-only">Personal agent on a phone, with the live agent-to-agent trace beside it</h1>
        {route === "cases" && session ? <CasesView /> : <Stage admin={admin} notice={notice} onSwitch={switchCustomer} />}
      </main>
    </>
  );
}
