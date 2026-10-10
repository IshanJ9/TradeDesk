// How it works: the LangGraph orchestrator, drawn and playable, then who owns what and the moments that slow
// you down. The copy describes only what the app does today (see README).
import { GraphDiagram } from "./GraphDiagram";
import { Link, SiteFooter, SiteHeader } from "./parts";

const STEPS: { who: "you" | "ai" | "code"; title: string; body: string }[] = [
  { who: "you", title: "You ask", body: "Type or speak: “sell half my Infosys and buy ITC with the money”. Spoken words land in the box for you to check; they're never sent on their own." },
  { who: "code", title: "Input guard", body: "A message that tries to change the rules (“ignore your limits”) is answered by code. The model never sees it." },
  { who: "code", title: "Router", body: "Code sends each message down one of five routes: read, risk, order, rule or plan. Each route allows only its own tools, so a question can never draft an order and an order request can't save a standing rule." },
  { who: "ai", title: "The model reads and drafts", body: "It looks up holdings, positions and prices through tools, and asks for a card. Every figure it puts in must be one you typed." },
  { who: "code", title: "The card is built and checked", body: "Code checks the price band, your holdings, cash and your own limits, adds every charge, and writes the card's words." },
  { who: "code", title: "Output guard", body: "If the reply claims an order was placed, gives advice, or uses a number not in your data, code replaces it." },
  { who: "you", title: "You approve, or decline", body: "Click Approve on that exact card. Its fingerprint must match, and code checks the price and your limits again. It's sent once to 021." },
];

const OWNS = [
  { who: "ai" as const, head: "can", items: ["Understand what you mean, in English or Hinglish", "Look up your holdings, positions and orders", "Ask for an order card or a multi-step plan", "Explain a card in plain words"] },
  { who: "code" as const, head: "always", items: ["Writes every card, every price and every charge", "Applies your own limits: orders a day, size, daily loss", "Expires a card after 60 seconds, re-quotes if the price moved over 1%", "Sends only the exact card you approved, and only once"] },
  { who: "never" as const, head: "nobody", items: ["Places an order without your approval", "Gives tips, predictions or “expected returns”", "Follows instructions hidden in stock names or messages", "Re-sends an order whose result is unknown"] },
];

const FAQ = [
  ["Can the AI place an order by itself?", "No. It has no tool that sends anything, and the broker connection it gets can only read. Sending needs your click on one exact card, through a route the AI can't reach."],
  ["What is the approval fingerprint?", "A code made from every detail of the order: stock, side, quantity, price, type and more. Approve sends it back. If anything changed, it doesn't match and nothing is sent."],
  ["What happens if the network drops while sending?", "We never send it again. We look for the order in 021's order book by what it looks like. If we can't confirm it, we say “not confirmed” and wait, instead of guessing."],
  ["Does it count orders I place in 021's own app?", "Yes. They appear in the “021 app” tab, marked as placed outside TradeDesk, and count toward your daily limits."],
  ["What if the app restarts?", "Cards and plans waiting for you are saved and come back with the same fingerprint. A plan that was running stops there, and says so; nothing unsent is sent."],
];

export function HowItWorks() {
  return (
    <div className="brand">
      <SiteHeader current="how" />
      <main className="b-wrap pb-4">
        <section className="b-rise flex max-w-[860px] flex-col gap-5 pb-14 pt-16 md:pt-20">
          <div className="b-eyebrow">How it works</div>
          <h1 className="m-0 text-[42px] font-normal leading-[1.08] md:text-[60px]">Copilot, <i>not autopilot.</i></h1>
          <p className="b-muted m-0 max-w-[720px] text-[18px]">
            You talk to TradeDesk in English or Hinglish. It reads your account and drafts orders. Nothing reaches the exchange until you approve one exact order card, and code, not the AI, enforces every money rule.
          </p>
          <div className="flex flex-wrap gap-4 text-[13px] b-muted">
            <span><span className="b-tag" data-who="you">YOU</span> your decision</span>
            <span><span className="b-tag" data-who="ai">AI</span> reads and drafts</span>
            <span><span className="b-tag" data-who="code">CODE</span> checks and enforces</span>
          </div>
        </section>

        <section aria-labelledby="graph" className="flex flex-col gap-5">
          <div>
            <div className="b-eyebrow">The orchestrator, live</div>
            <h2 id="graph" className="m-0 mt-2 text-[30px] font-normal md:text-[34px]">Watch one message travel through the graph</h2>
            <p className="b-muted mb-0 mt-2 max-w-[760px]">
              TradeDesk runs its assistant as a LangGraph state graph. Each box below is a real node. Pick an example and see which checks it passes, and where code stops it.
            </p>
          </div>
          <GraphDiagram />
        </section>

        <section aria-labelledby="steps" className="mt-20 flex flex-col gap-5">
          <div>
            <div className="b-eyebrow">The path of one order</div>
            <h2 id="steps" className="m-0 mt-2 text-[30px] font-normal md:text-[34px]">From your words to an order, in seven steps</h2>
          </div>
          <ol className="m-0 grid list-none gap-3.5 p-0 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s.title} className="b-panel flex flex-col gap-2.5 p-5" style={i === STEPS.length - 1 ? { boxShadow: "inset 0 0 0 1px var(--b-violet)" } : undefined}>
                <div className="flex items-center"><span className="serif n text-[24px]" style={{ color: i === STEPS.length - 1 ? "var(--b-info)" : "var(--b-faint)" }}>0{i + 1}</span><span className="flex-1" /><span className="b-tag" data-who={s.who}>{s.who.toUpperCase()}</span></div>
                <h3 className="m-0 text-[17px] font-bold">{s.title}</h3>
                <p className="b-muted m-0 text-[14px]">{s.body}</p>
              </li>
            ))}
            <li className="flex flex-col justify-center gap-2.5 rounded-2xl p-5" style={{ background: "linear-gradient(135deg, var(--b-tag-ai), var(--b-glow-2))", boxShadow: "inset 0 0 0 1px var(--b-line)" }}>
              <div className="b-eyebrow">See it in the app</div>
              <p className="b-muted m-0 text-[14px]">Every step shows up in the desk's Assistant tab as it happens, with its time in milliseconds.</p>
              <Link to="app" className="b-link inline-flex min-h-[44px] items-center text-[14px]">Open the desk →</Link>
            </li>
          </ol>
        </section>

        <section aria-labelledby="owns" className="mt-20 flex flex-col gap-5">
          <div>
            <div className="b-eyebrow">Who owns what</div>
            <h2 id="owns" className="m-0 mt-2 text-[30px] font-normal md:text-[34px]">The AI talks. Code decides what's allowed.</h2>
          </div>
          <div className="grid gap-3.5 md:grid-cols-3">
            {OWNS.map((o) => (
              <div key={o.head} className="b-panel p-6">
                <div className="mb-3.5 flex items-center gap-2.5"><span className="b-tag" data-who={o.who}>{o.who === "never" ? "NEVER" : o.who.toUpperCase()}</span><span className="font-bold">{o.head}</span></div>
                <ul className="b-muted m-0 flex flex-col gap-2 pl-[18px] text-[14px]">{o.items.map((t) => <li key={t}>{t}</li>)}</ul>
              </div>
            ))}
          </div>
        </section>

        <section aria-labelledby="net" className="mt-20 flex flex-col gap-5">
          <div>
            <div className="b-eyebrow">When things change</div>
            <h2 id="net" className="m-0 mt-2 text-[30px] font-normal md:text-[34px]">Three moments where TradeDesk slows you down</h2>
          </div>
          <div className="grid gap-3.5 md:grid-cols-3">
            <div className="b-panel p-6">
              <div className="text-[18px] font-bold">The price moved</div>
              <p className="b-muted mb-3.5 mt-2 text-[14px]">Over 1% since you saw the card? Nothing is sent, and a fresh card replaces it. You approve the new price, never the old one.</p>
              <div className="rounded-[10px] px-3 py-2.5 text-[13px]" style={{ background: "var(--b-warn-bg)", color: "var(--b-warn-ink)" }}>The price moved from <span className="n">₹1,448.40</span> to <span className="n">₹1,465.10</span> (1.2%). Nothing was sent.</div>
            </div>
            <div className="b-panel p-6">
              <div className="text-[18px] font-bold">You crossed your own limit</div>
              <p className="b-muted mb-3.5 mt-2 text-[14px]">The card states the fact in your own numbers, and Approve stays off until you tick “I've read this”. Hard limits, if you switch them on, refuse outright.</p>
              <div className="rounded-[10px] px-3 py-2.5 text-[13px]" style={{ background: "var(--b-warn-bg)", color: "var(--b-warn-ink)" }}>You set <span className="n">5</span> orders a day; this would be order <span className="n">#6</span>.</div>
            </div>
            <div className="b-panel p-6">
              <div className="text-[18px] font-bold">The network dropped</div>
              <p className="b-muted mb-3.5 mt-2 text-[14px]">We check 021's order book instead of sending again. If we can't confirm it, we say so plainly.</p>
              <div className="rounded-[10px] px-3 py-2.5 text-[13px]" style={{ border: "1px dashed var(--b-muted)" }}>Not confirmed, and not re-sent. Check your order book.</div>
            </div>
          </div>
        </section>

        <section aria-labelledby="faq" className="mt-20 flex flex-col gap-3.5">
          <div>
            <div className="b-eyebrow">Questions</div>
            <h2 id="faq" className="m-0 mb-2 mt-2 text-[30px] font-normal md:text-[34px]">Things people ask first</h2>
          </div>
          {FAQ.map(([q, a], i) => (
            <details key={q} className="b-panel px-5 py-4" open={i === 0}>
              <summary className="min-h-[28px] font-bold">{q}</summary>
              <p className="b-muted mb-0 mt-2.5">{a}</p>
            </details>
          ))}
        </section>

        <section className="mt-20 flex flex-wrap items-center gap-6 rounded-[20px] p-8 md:p-12" style={{ background: "linear-gradient(135deg, var(--b-tag-ai), var(--b-glow-2))", boxShadow: "inset 0 0 0 1px var(--b-line)" }}>
          <div className="min-w-[260px] flex-1">
            <h2 className="m-0 text-[28px] font-normal md:text-[32px]">Set your limits, then <i>ask anything.</i></h2>
            <p className="b-muted mb-0 mt-2">Log in and open the desk.</p>
          </div>
          <Link to="login" className="b-btn b-btn-primary">Get started</Link>
          <Link to="app" className="b-btn b-btn-ghost">Open the desk</Link>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}
