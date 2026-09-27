import { Loader2, Monitor, Power } from "lucide-react";
import { useCore } from "../core";
import { haptic } from "../lib/haptics";
import { Desktop } from "../components/Desktop";

/**
 * The Computer page.
 *
 * This is now a desktop rather than a tabbed panel.  The old version had a tab
 * strip pinned above the content, which meant the live screen could never own
 * the full height of the panel -- the top of the remote Chrome, which is its own
 * tab strip and address bar, sat underneath a second row of RAG Agents tabs.
 *
 * The tabbed arrangement also had a "Browser" tab that showed a CDP screenshot
 * of the remote browser.  That is gone: a still image of a browser is not a
 * browser, and having it sit next to the real screen invited people to use the
 * wrong one.  The desktop's Browser icon opens the live screen directly.
 *
 * The agent's CDP automation is untouched -- it still drives Chrome, and
 * `useCore` still receives frames from it -- it is simply no longer a thing a
 * person is offered as a view of the machine.
 */
export function ComputerView() {
  const computer = useCore((s) => s.computer);
  const computerStart = useCore((s) => s.computerStart);

  if (computer.running) return <Desktop />;

  if (computer.booting) {
    return (
      <div className="computer">
        <div className="comp__bootcard">
          <div className="comp__bootbox">
            <div className="comp__bootorb" style={{ animation: "pulse 1.2s ease infinite" }}>
              <Loader2 size={38} style={{ animation: "spin 1s linear infinite" }} />
            </div>
            <div className="comp__bootbody">Booting your computer…</div>
            <div className="comp__bootsub">Starting the display, browser and terminal.</div>
            <div className="boot-progress">
              <i style={{ width: "62%" }} />
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="computer">
      <div className="comp__bootcard">
        <div className="comp__bootbox">
          <div className="comp__bootorb">
            <Monitor size={38} />
          </div>
          <div className="comp__bootbody">The workforce computer is offline</div>
          <div className="comp__bootsub">
            Bring up the remote computer so the Engineer, Navigator and their tools get a real machine to work
            on — browser, files and terminal.
          </div>
          <button className="btn btn--primary btn--lg" onClick={() => (haptic("heavy"), computerStart())}>
            <Power size={17} /> Boot the computer
          </button>
        </div>
      </div>
    </div>
  );
}
