import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useSession } from "../api/session";
import type { SessionInfo } from "../api/types";
import { useA2aFeed } from "../hooks/useA2aFeed";
import { useConversation } from "../hooks/useConversation";
import { useFitScale } from "../hooks/useFitScale";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { useWriteback } from "../hooks/useWriteback";
import { normalizePersona } from "../lib/normalize";
import { buildTimeline } from "../lib/timeline";
import { ChatScreen } from "./phone/ChatScreen";
import { PersonaPicker } from "./phone/PersonaPicker";
import { PhoneFrame } from "./phone/PhoneFrame";
import { TracePanel } from "./trace/TracePanel";

interface SignedInProps {
  session: SessionInfo;
  scale: number;
  onSwitch: () => void;
}

/** Everything that exists only for a signed-in customer: the chat on the phone and the live trace beside it. */
function SignedInStage({ session, scale, onSwitch }: SignedInProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const [hot, setHot] = useState(false);

  const feed = useA2aFeed(hot);
  const convo = useConversation(feed.events, feed.nudge);
  const wb = useWriteback(feed.events);

  // The A2A feed polls quickly while ALDO bot works or a size update from ALDO is still owed.
  const waiting = wb.writeback === "waiting";
  useEffect(() => {
    setHot(convo.busy || waiting);
  }, [convo.busy, waiting]);

  const items = useMemo(() => buildTimeline(feed.events, convo.runs), [feed.events, convo.runs]);
  const persona = useMemo(() => normalizePersona(session.customer), [session.customer]);

  return (
    <>
      <section className="phone-col" aria-label="ALDO bot phone">
        <PhoneFrame scale={scale} inert={modalOpen}>
          <ChatScreen
            customer={{
              customerId: persona.customerId,
              name: persona.displayName,
              tier: persona.tier,
              tierLabel: persona.tierLabel,
              locationLabel: persona.locationLabel,
            }}
            convo={convo}
            a2a={feed.events}
            writeback={wb.writeback}
            onCheckAgain={wb.checkAgain}
            onNudge={feed.nudge}
            onSwitch={onSwitch}
            onModalChange={setModalOpen}
          />
        </PhoneFrame>
      </section>
      <div className="trace-col" inert={modalOpen}>
        <TracePanel items={items} signedIn live={hot} />
      </div>
    </>
  );
}

interface Props {
  admin: boolean;
  notice?: string;
  onSwitch: () => void;
}

/**
 * The stage: the phone on the left (scaled to fit the viewport height, no page scroll) and the live
 * A2A trace on the right. Below 1100 px the two stack.
 */
export function Stage({ admin, notice, onSwitch }: Props) {
  const session = useSession();
  const qc = useQueryClient();
  const wide = useMediaQuery("(min-width: 1100px)");
  const stageRef = useRef<HTMLDivElement>(null);
  const scale = useFitScale(stageRef, wide);

  return (
    <div className="stage" ref={stageRef} data-testid="stage" data-layout={wide ? "wide" : "stacked"}>
      {session ? (
        <SignedInStage key={session.customer.customer_id} session={session} scale={scale} onSwitch={onSwitch} />
      ) : (
        <>
          <section className="phone-col" aria-label="ALDO bot phone">
            <PhoneFrame scale={scale}>
              <PersonaPicker admin={admin} notice={notice} onReset={() => void qc.resetQueries()} />
            </PhoneFrame>
          </section>
          <div className="trace-col">
            <TracePanel items={[]} signedIn={false} live={false} />
          </div>
        </>
      )}
    </div>
  );
}
