// Voice I/O via the Web Speech API: speech-to-text (dictation) and
// text-to-speech (say).  With a calm, predictable fallback everywhere else
// (buttons hidden on unsupported devices).

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
  rec.continuous = false;
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

/* ---- text-to-speech --------------------------------------------------------- */

export function ttsSupported(): boolean {
  return typeof window !== "undefined" && !!window.speechSynthesis;
}

let currentUtterance: SpeechSynthesisUtterance | null = null;

export function speak(text: string, opts?: { onEnd?: () => void; onStart?: () => void }): void {
  if (!ttsSupported()) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  u.rate = 1.05;
  u.pitch = 1;
  u.onstart = () => {
    opts?.onStart?.();
  };
  u.onend = () => {
    currentUtterance = null;
    opts?.onEnd?.();
  };
  u.onerror = () => {
    currentUtterance = null;
  };
  currentUtterance = u;
  window.speechSynthesis.speak(u);
}

export function stopSpeaking(): void {
  if (!ttsSupported()) return;
  window.speechSynthesis.cancel();
  currentUtterance = null;
}

export function isSpeaking(): boolean {
  return currentUtterance !== null || (ttsSupported() && window.speechSynthesis.speaking);
}
