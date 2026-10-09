// The public front page. Every claim here is something the app does today (see README).
import { CardPreview, Link, SiteFooter, SiteHeader } from "./parts";

const LEVELS = [
  { tag: "Ask", say: "What's my P&L today?", does: "Answers from your account. Every number comes from 021's data, never typed by the AI." },
  { tag: "Order", say: "Buy 10 Infosys at 1450", does: "Drafts one exact card with every charge. Nothing is sent until you approve it." },
  { tag: "Rule", say: "Buy 5 TCS if it falls below 3800", does: "Watches the price. When it triggers, it prepares a card for you; it never trades on its own." },
  { tag: "Portfolio", say: "Exit all my losing intraday positions", does: "One plan for the whole portfolio. Code finds the positions and works out every quantity." },
];

const MOMENTS = [
  { title: "The price moved", body: "If it moved more than 1% after you saw the card, nothing is sent and a fresh card takes its place." },
  { title: "The network lost the reply", body: "We look for the order in 021's order book instead of sending it again. Never two orders." },
  { title: "Hidden instructions in data", body: "A stock name that says “ignore your rules” is treated as plain text, and shown to you." },
  { title: "A vague request", body: "“Buy 10 Tata” gets a question: which one? A number you never typed is never used." },
];

const ALSO = [
  ["Your own limits", "Orders a day, order size, daily loss. Cross one and you tick “I've read this” first; switch a limit to hard and the app refuses."],
  ["Voice", "Speak instead of typing. The words land in the box for you to check; nothing is sent until you do."],
  ["In sync with 021's app", "Orders you place in 021's own app show up here and count toward your limits."],
  ["Watch it think", "Every step the assistant takes, every check it passes, shows up live with its time."],
];

export function Landing() {
  return (
    <div className="brand">
      <SiteHeader current="landing" />
      <main>
        {/* hero */}
        <section className="b-wrap grid items-center gap-12 pb-20 pt-16 md:pt-24 lg:grid-cols-[1.15fr_1fr]">
          <div className="b-rise flex flex-col gap-6">
            <div className="b-eyebrow">An AI trading copilot for 021 Trade</div>
            <h1 className="m-0 text-[44px] font-normal leading-[1.06] md:text-[64px]">
              Copilot, <i>not autopilot.</i>
            </h1>
            <p className="b-muted m-0 max-w-[560px] text-[18px]">
              Ask about your account or describe an order, in English or Hinglish. TradeDesk drafts it. You approve the exact card. Nothing reaches the exchange without your click.
            </p>
            <div className="flex flex-wrap gap-3">
              <Link to="login" className="b-btn b-btn-primary">Get started</Link>
              <Link to="how" className="b-btn b-btn-ghost">See how it works</Link>
            </div>
            <ul className="m-0 flex list-none flex-wrap gap-2.5 p-0 text-[13.5px]">
              {[["var(--b-gain)", "Nothing is sent without your approval"], ["var(--b-info)", "Code owns every money rule"], ["var(--b-chart)", "An order is never sent twice"]].map(([c, t]) => (
                <li key={t} className="inline-flex min-h-[36px] items-center gap-2 rounded-[10px] px-3.5" style={{ background: "var(--b-surface-2)", boxShadow: "inset 0 0 0 1px var(--b-line)" }}>
                  <span className="g-dot" style={{ background: c }} />{t}
                </li>
              ))}
            </ul>
          </div>
          <div className="b-rise relative mx-auto w-full max-w-[420px]" style={{ animationDelay: "120ms" }}>
            <div className="b-quote mb-3 ml-auto w-fit">Buy 10 Infosys at 1450</div>
            <CardPreview />
            <p className="b-muted mt-3 text-center text-[13px]">The AI drafted it. Only your click can send it.</p>
          </div>
        </section>

        {/* four levels */}
        <section className="b-wrap" aria-labelledby="levels">
          <div className="b-eyebrow">What you can say</div>
          <h2 id="levels" className="mb-6 mt-2 text-[30px] font-normal md:text-[36px]">From a question to your whole portfolio</h2>
          <ol className="m-0 grid list-none gap-3.5 p-0 sm:grid-cols-2 lg:grid-cols-4">
            {LEVELS.map((l, i) => (
              <li key={l.tag} className="b-panel flex flex-col gap-3 p-5">
                <div className="flex items-center"><span className="serif n text-[24px]" style={{ color: "var(--b-faint)" }}>0{i + 1}</span><span className="flex-1" /><span className="b-tag" data-who="you">{l.tag}</span></div>
                <div className="b-quote w-fit">{l.say}</div>
                <p className="b-muted m-0 text-[14px]">{l.does}</p>
              </li>
            ))}
          </ol>
        </section>

        {/* failure moments */}
        <section className="b-wrap mt-20" aria-labelledby="moments">
          <div className="b-eyebrow">Built for the moments that go wrong</div>
          <h2 id="moments" className="mb-6 mt-2 text-[30px] font-normal md:text-[36px]">It slows down exactly when it should</h2>
          <div className="grid gap-3.5 sm:grid-cols-2">
            {MOMENTS.map((m) => (
              <div key={m.title} className="b-panel p-6">
                <div className="text-[18px] font-bold">{m.title}</div>
                <p className="b-muted mb-0 mt-2 text-[14px]">{m.body}</p>
              </div>
            ))}
          </div>
        </section>

        {/* also */}
        <section className="b-wrap mt-20" aria-labelledby="also">
          <div className="b-eyebrow">On your side</div>
          <h2 id="also" className="mb-6 mt-2 text-[30px] font-normal md:text-[36px]">Facts and your own rules. <i>Never advice.</i></h2>
          <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-4">
            {ALSO.map(([t, b]) => (
              <div key={t} className="b-panel p-5">
                <div className="font-bold">{t}</div>
                <p className="b-muted mb-0 mt-2 text-[14px]">{b}</p>
              </div>
            ))}
          </div>
        </section>

        {/* proof */}
        <section className="b-wrap mt-20" aria-label="How it was tested">
          <div className="grid gap-3.5 sm:grid-cols-3">
            {[["1,000+", "automated tests, and every safety check broken on purpose to prove a test catches it"], ["0 failures", "on 31 awkward and adversarial prompts run against the real AI model"], ["Live", "tested on 021's sandbox: orders, partial fills, modify, cancel, and network failures with no duplicate"]].map(([big, small]) => (
              <div key={big} className="b-panel p-6">
                <div className="serif n text-[34px] leading-none">{big}</div>
                <p className="b-muted mb-0 mt-3 text-[14px]">{small}</p>
              </div>
            ))}
          </div>
        </section>

        {/* call to action */}
        <section className="b-wrap mt-20">
          <div className="flex flex-wrap items-center gap-6 rounded-[20px] p-8 md:p-12" style={{ background: "linear-gradient(135deg, var(--b-tag-ai), var(--b-glow-2))", boxShadow: "inset 0 0 0 1px var(--b-line)" }}>
            <div className="min-w-[260px] flex-1">
              <h2 className="m-0 text-[28px] font-normal md:text-[32px]">Read, draft, approve. <i>You do the last one.</i></h2>
              <p className="b-muted mb-0 mt-2">See how a message travels from your words to an order.</p>
            </div>
            <Link to="how" className="b-btn b-btn-ghost">How it works</Link>
            <Link to="login" className="b-btn b-btn-primary">Get started</Link>
          </div>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}
