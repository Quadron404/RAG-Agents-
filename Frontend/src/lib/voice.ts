// Speech-to-text via the Web Speech API where supported, with a calm,
// predictable fallback everywhere else (button hidden on unsupported devices).
//
// Text-to-speech is layered on top so a "call" session can both listen
// (STT) and speak back (TTS) without coupling either to the chat transport.

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  maxAlternatives: number;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: {
    resultIndex: number;
    results: ArrayLike<{ isFinal: boolean; item: (i: number) => { transcript: string } }>;
  }) => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error: string }) => void) | null;
};

export function speechSupported(): boolean {
  return typeof window !== "undefined" && !!getRecognitionCtor();
}

function getRecognitionCtor(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: new () => SpeechRecognitionLike;
    webkitSpeechRecognition?: new () => SpeechRecognitionLike;
  };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export interface DictationHandle {
  stop: () => void;
}

export function startDictation(opts: {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onEnd: () => void;
  onError?: (msg: string) => void;
}): DictationHandle | null {
  const Ctor = getRecognitionCtor();
  if (!Ctor) return null;
  const rec = new Ctor();
  rec.lang = "en-US";
  rec.continuous = true;
  rec.interimResults = true;
  rec.maxAlternatives = 1;

  rec.onresult = (event) => {
    let interim = "";
    let final = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const r = event.results[i];
      const text = r.item(0).transcript;
      if (r.isFinal) final += text;
      else interim += text;
    }
    if (final) opts.onFinal(final);
    if (interim) opts.onInterim(interim);
  };
  rec.onerror = (event) => {
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      opts.onError?.("Microphone access is unavailable.");
    } else if (event.error !== "aborted" && event.error !== "no-speech") {
      opts.onError?.(`Voice input stopped: ${event.error}`);
    }
  };
  rec.onend = () => opts.onEnd();

  try {
    rec.start();
  } catch {
    opts.onEnd();
    return null;
  }
  return { stop: () => rec.stop() };
}

/* ---- text-to-speech ------------------------------------------------------- */

export function speechSynthesisSupported(): boolean {
  return typeof window !== "undefined" && !!window.speechSynthesis;
}

let currentUtterance: SpeechSynthesisUtterance | null = null;
let voicesReady = false;
let voices: SpeechSynthesisVoice[] = [];

function loadVoices(): void {
  if (typeof window === "undefined" || !window.speechSynthesis) return;
  const v = window.speechSynthesis.getVoices();
  if (v && v.length) {
    voices = v;
    voicesReady = true;
  }
}

// Some browsers populate voices asynchronously.  Listen for the
// voiceschanged event and re-load so a voice can be selected.
if (typeof window !== "undefined" && window.speechSynthesis) {
  window.speechSynthesis.onvoiceschanged = () => loadVoices();
  loadVoices();
}

function pickVoice(): SpeechSynthesisVoice | undefined {
  // Prefer a clear English female voice; fall back to any English voice,
  // then to the first available voice.
  const prefer = (lang: string) =>
    voices.find(
      (v) =>
        v.lang.startsWith(lang) &&
        (v.name.toLowerCase().includes("female") ||
          v.name.toLowerCase().includes("woman") ||
          v.name.toLowerCase().includes("samantha") ||
          v.name.toLowerCase().includes("karen") ||
          v.name.toLowerCase().includes("allison")),
    );
  const english = prefer("en");
  if (english) return english;
  return voices.find((v) => v.lang.startsWith("en")) ?? voices[0];
}

/** Speak `text`, cancelling any utterance already in flight. */
export function speak(text: string): void {
  if (typeof window === "undefined" || !window.speechSynthesis) return;
  stopSpeech();
  loadVoices();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  u.rate = 1;
  u.pitch = 1;
  u.volume = 1;
  const voice = pickVoice();
  if (voice) u.voice = voice;
  u.onend = () => {
    if (currentUtterance === u) currentUtterance = null;
  };
  u.onerror = () => {
    if (currentUtterance === u) currentUtterance = null;
    // A voice-selection failure is not fatal; retry with the default voice.
    if (!voice) {
      u.voice = null;
      try {
        window.speechSynthesis.speak(u);
      } catch {
        /* best effort */
      }
    }
  };
  currentUtterance = u;
  try {
    window.speechSynthesis.speak(u);
  } catch {
    currentUtterance = null;
  }
}

/** Cancel any in-flight speech. */
export function stopSpeech(): void {
  if (typeof window !== "undefined" && window.speechSynthesis) {
    window.speechSynthesis.cancel();
  }
  currentUtterance = null;
}

export function isSpeaking(): boolean {
  return typeof window !== "undefined" && !!window.speechSynthesis && window.speechSynthesis.speaking;
}

/** Whether TTS voices have been enumerated and a voice can be selected. */
export function ttsReady(): boolean {
  return voicesReady || voices.length > 0;
}

/** The name of the currently selected TTS voice, or "default". */
export function ttsVoiceName(): string {
  return pickVoice()?.name ?? "default";
}
