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
  const lastUserText = useRef<string | null>(null);
  const lastSeenDelta = useRef<string | null>(null);
  const startListeningRef = useRef<(() => void) | null>(null);

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
    lastUserText.current = null;
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = null;
    endCall();
    haptic("heavy");
  }, [clearListening, stopSpeech, endCall, haptic]);

  const startListening = useCallback(() => {
    if (!callActive || !speechSupported() || isSpeaking()) return;
    if (dictRef.current) return;
    if (micMuted) return;
    setCallState("listening");
    setListeningUI(true);
    setSpeakingUI(false);
    dictRef.current = startDictation({
      onInterim: () => {
        /* visual feedback only */
      },
      onFinal: (t: string) => {
        if (!t.trim()) return;
        const text = t.trim();
        haptic("medium");
        lastUserText.current = text;
        lastSpoken.current = null;
        addCallMessage({ id: "c-" + Date.now(), role: "user", content: text, created_at: Date.now() / 1000 });
        if (activeThreadId) {
          void send(text);
        }
        clearListening();
        setCallState("speaking");
        setSpeakingUI(true);
      },
      onEnd: () => {
        if (callActive && !speakingUI && !micMuted && !isSpeaking()) {
          setTimeout(() => {
            if (callActive && (callState === "listening" || callState === "connected")) {
              startListeningRef.current?.();
            }
          }, 400);
        }
      },
      onError: () => {
        clearListening();
        if (callActive && !isSpeaking()) {
          setCallState("connected");
          setListeningUI(false);
          setTimeout(() => startListeningRef.current?.(), 1000);
        }
      },
    });
  }, [
    callActive,
    speechSupported,
    isSpeaking,
    micMuted,
    clearListening,
    addCallMessage,
    activeThreadId,
    send,
    speakingUI,
  ]);

  startListeningRef.current = startListening;

  // When TTS starts (via API), mark speaking state so listening is paused.
  useEffect(() => {
    if (!callActive) return;
    const speaking = isSpeaking();
    if (speaking && !speakingUI) {
      setSpeakingUI(true);
      setListeningUI(false);
      setCallState("speaking");
      clearListening();
    }
  }, [callActive, isSpeaking(), speakingUI, clearListening, setCallState]);

  // Capture the latest non-empty commanderDelta as the assistant streams.
  useEffect(() => {
    const delta = run?.commanderDelta?.trim();
    if (delta) lastSeenDelta.current = delta;
  }, [run]);

  // Speak the assistant's finished response when the run completes.
  // core.ts clears commanderDelta to "" when phase becomes "done", so we
  // read it from lastSeenDelta instead.
  useEffect(() => {
    if (!callActive) return;
    if (!run || run.phase !== "done") return;
    const delta = lastSeenDelta.current;
    if (!delta || delta === lastSpoken.current) return;
    lastSpoken.current = delta;
    lastSeenDelta.current = null;
    addCallMessage({ id: "c-" + Date.now(), role: "assistant", content: delta, created_at: Date.now() / 1000 });
    setCallState("speaking");
    setSpeakingUI(true);
    setListeningUI(false);
    void speak(delta);
  }, [callActive, run, run?.phase, addCallMessage, setCallState]);

  // When TTS finishes, resume listening.
  useEffect(() => {
    if (!callActive) return;
    const speaking = isSpeaking();
    if (!speaking && speakingUI) {
      setSpeakingUI(false);
      setCallState("listening");
      setTimeout(() => startListeningRef.current?.(), 300);
    }
  }, [callActive, speakingUI, setCallState]);

  const toggleMute = useCallback(() => {
    setMicMuted((p) => {
      const next = !p;
      if (next) clearListening();
      else startListeningRef.current?.();
      return next;
    });
  }, [clearListening, startListeningRef, setMicMuted]);

  // Auto-start listening when the call connects.
  useEffect(() => {
    if (callActive && callState === "connected" && !dictRef.current && !isSpeaking()) {
      startListeningRef.current?.();
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
