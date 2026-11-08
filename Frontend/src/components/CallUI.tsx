import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, MicOff, Phone, VolumeX } from "lucide-react";
import { useUi } from "../store";
import { useCore } from "../core";
import {
  speak,
  stopSpeech,
  isSpeaking,
  speechSupported,
  startDictation,
} from "../lib/voice";
import { haptic } from "../lib/haptics";

function CallUI() {
  const {
    callActive,
    callState,
    callDuration,
    callTranscript,
    setCallState,
    setCallDuration,
    addCallMessage,
    endCall,
  } = useUi();
  const { send, stopRun, activeThreadId, run } = useCore();

  const [localDuration, setLocalDuration] = useState(callDuration);
  const [micMuted, setMicMuted] = useState(false);
  const [listeningUI, setListeningUI] = useState(false);
  const [speakingUI, setSpeakingUI] = useState(false);
  const dictRef = useRef<{ stop: () => void } | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const durationRef = useRef(callDuration);
  const lastSpoken = useRef<string | null>(null);
  const lastSeenDelta = useRef<string | null>(null);
  const speakingRef = useRef(isSpeaking());
  // suppressSTT is a synchronous ref so it guards against race conditions
  // where onEnd fires before React state updates take effect.
  const suppressSTT = useRef(false);
  // ttsRef tracks whether TTS audio is actively playing (set before fetch starts).
  const ttsRef = useRef(false);

  // Mirror store duration into local state for the UI timer.
  useEffect(() => {
    setLocalDuration(callDuration);
    durationRef.current = callDuration;
  }, [callDuration]);

  // Call lifecycle timer.
  useEffect(() => {
    if (callActive && callState === "connected") {
      if (!timerRef.current) {
        timerRef.current = setInterval(() => {
          const next = durationRef.current + 1;
          durationRef.current = next;
          setCallDuration(next);
        }, 1000);
      }
    } else {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
    };
  }, [callActive, callState, setCallDuration]);

  const formatTime = (s: number) => {
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, "0")}`;
  };

  const clearListening = useCallback(() => {
    dictRef.current?.stop();
    dictRef.current = null;
    setListeningUI(false);
  }, []);

  const endCallHandler = useCallback(() => {
    clearListening();
    stopSpeech();
    lastSpoken.current = null;
    suppressSTT.current = false;
    ttsRef.current = false;
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    endCall();
    haptic("heavy");
  }, [clearListening, stopSpeech, endCall, haptic]);

  const startListening = useCallback(() => {
    if (!callActive || !speechSupported() || isSpeaking() || suppressSTT.current || ttsRef.current) return;
    if (dictRef.current) return;
    if (micMuted) return;
    setCallState("listening");
    setListeningUI(true);
    setSpeakingUI(false);
    suppressSTT.current = false;
    ttsRef.current = false;
    dictRef.current = startDictation({
      onInterim: () => {
        /* visual feedback only */
      },
      onFinal: (t: string) => {
        // Safety net: ignore results while Chief is speaking or STT is suppressed.
        if (suppressSTT.current || isSpeaking() || ttsRef.current) return;
        if (!t.trim()) return;
        const text = t.trim();
        // Reject near-identical echoes of what Chief just said.
        const last = lastSpoken.current;
        if (last && text.toLowerCase() === last.toLowerCase()) return;
        lastSpoken.current = null;
        addCallMessage({ id: "c-" + Date.now(), role: "user", content: text, created_at: Date.now() / 1000 });
        void send(text);
        // Stop listening while the AI replies (prevents echo).
        // Set speaking state BEFORE stopping dictation so onEnd cannot restart.
        setSpeakingUI(true);
        setCallState("speaking");
        suppressSTT.current = true;
        clearListening();
        setListeningUI(false);
      },
      onEnd: () => {
        // Auto-restart listening only when not speaking and call is active.
        // suppressSTT is checked first (synchronous ref) to prevent races.
        if (!suppressSTT.current && callActive && !speakingUI && !micMuted && !isSpeaking() && !ttsRef.current) {
          setTimeout(() => {
            if (!suppressSTT.current && callActive && (callState === "listening" || callState === "connected") && !isSpeaking() && !ttsRef.current) {
              startListeningRef.current();
            }
          }, 400);
        }
      },
      onError: () => {
        clearListening();
        if (!suppressSTT.current && callActive && !isSpeaking() && !ttsRef.current) {
          setCallState("connected");
          setListeningUI(false);
          setTimeout(() => startListeningRef.current(), 1000);
        }
      },
    });
  }, [callActive, speechSupported, isSpeaking, dictRef, micMuted, setCallState, setListeningUI, setSpeakingUI, addCallMessage, activeThreadId, send, clearListening]);

  const startListeningRef = useRef(startListening);
  startListeningRef.current = startListening;

  // Speak the assistant's finished response when the run completes.
  // core.ts clears commanderDelta to "" when phase becomes "done", so we
  // capture it during streaming via lastSeenDelta.
  useEffect(() => {
    const delta = run?.commanderDelta?.trim();
    if (delta) lastSeenDelta.current = delta;
  }, [run]);

  useEffect(() => {
    if (!callActive) return;
    if (!run || run.phase !== "done") return;
    const delta = lastSeenDelta.current;
    if (!delta || delta === lastSpoken.current) return;
    lastSpoken.current = delta;
    lastSeenDelta.current = null;
    addCallMessage({ id: "c-" + Date.now(), role: "assistant", content: delta, created_at: Date.now() / 1000 });
    suppressSTT.current = true;
    ttsRef.current = true;
    clearListening();
    setCallState("speaking");
    setSpeakingUI(true);
    setListeningUI(false);
    void speak(delta, () => {
      ttsRef.current = false;
      speakingRef.current = false;
      setSpeakingUI(false);
      // Cooldown so speaker audio has faded before mic re-opens.
      setTimeout(() => {
        suppressSTT.current = false;
        setCallState("listening");
        if (callActive) startListeningRef.current();
      }, 1000);
    });
  }, [callActive, run, run?.phase, addCallMessage, setCallState]);

  const toggleMute = useCallback(() => {
    setMicMuted((p) => {
      const next = !p;
      if (next) {
        suppressSTT.current = true;
        ttsRef.current = true;
        clearListening();
      } else {
        suppressSTT.current = false;
        ttsRef.current = false;
        startListeningRef.current();
      }
      return next;
    });
  }, [clearListening, setMicMuted]);

  // Auto-start listening when the call connects.
  useEffect(() => {
    if (callActive && callState === "connected" && !dictRef.current && !isSpeaking() && !suppressSTT.current && !ttsRef.current) {
      startListeningRef.current();
    }
  }, [callActive, callState]);

  if (!callActive) return null;

  return (
    <div className="call-overlay">
      <div className="call-backdrop" />
      <div className="call-panel">
        <div className="call-header">
          <button className="call-icbtn" onClick={endCallHandler} aria-label="End call" title="End call">
            <Phone size={20} />
          </button>
          <span className="call-status">
            {callState === "ringing"
              ? "Calling"
              : callState === "listening"
                ? "Listening"
                : callState === "speaking"
                  ? "Chief is speaking"
                  : "Connected"}
          </span>
          <button className="call-icbtn" onClick={toggleMute} aria-label={micMuted ? "Unmute" : "Mute"} title={micMuted ? "Unmute mic" : "Mute mic"}>
            {micMuted ? <MicOff size={18} /> : <Mic size={18} />}
          </button>
        </div>

        <div className="call-body">
          <div className="call-viz">
            <div className="call-viz__ring" />
            {Array.from({ length: 12 }).map((_, i) => (
              <div key={i} className={`call-viz__bar ${listeningUI || speakingUI ? "call-viz__bar--active" : ""}`} />
            ))}
            <div className="call-viz__center" />
          </div>
          <div className="call-time">{formatTime(localDuration)}</div>
          <div className="call-hint">
            {callState === "ringing"
              ? "Calling Chief…"
              : micMuted
                ? "Microphone muted"
                : callState === "listening"
                  ? "Speak now — I'm listening"
                  : callState === "speaking"
                    ? "Chief is replying"
                    : "Tap to speak"}
          </div>
        </div>

        <div className="call-transcript">
          <div className="call-transcript__head">
            <span className="call-transcript__label">Call transcript</span>
            <button className="call-icbtn call-icbtn--small" onClick={stopRun} title="Stop">
              <VolumeX size={14} />
            </button>
          </div>
          <div className="call-transcript__list">
            {callTranscript.map((m) => (
              <div key={m.id} className={`call-msg call-msg--${m.role}`}>
                <span className="call-msg__who">{m.role === "user" ? "You" : "Chief"}:</span>
                <span className="call-msg__text">{m.content}</span>
              </div>
            ))}
            {callTranscript.length === 0 && <div className="call-msg--empty">No words yet — start speaking.</div>}
          </div>
        </div>

        <div className="call-footer">
          <button
            className={`call-end ${callState === "speaking" ? "call-end--busy" : ""}`}
            onClick={endCallHandler}
            disabled={callState === "speaking"}
            aria-label="End call"
            title="End call"
          >
            <span className="call-end__ic">
              <Phone size={22} />
            </span>
            <span className="call-end__label">End call</span>
          </button>
        </div>
      </div>
    </div>
  );
}

export default CallUI;
