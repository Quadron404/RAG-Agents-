// Voice I/O for the call system.
//
// STT (speech-to-text) via the Web Speech API lets the user speak during a
// call.  TTS (text-to-speech) uses the backend API endpoint `/tts` (Cloudflare
// Workers AI `@cf/myshell-ai/melotts`) instead of the browser's built-in
// synthesis, so the AI's voice is consistent and doesn't depend on the local
// voice inventory.
//
// Echo prevention: STT listening is paused whenever TTS audio is playing, so
// the AI's own voice is never picked up by the mic and re-sent.

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

/* ---- text-to-speech (API) ------------------------------------------------- */

let currentAudio: HTMLAudioElement | null = null;

/** Speak `text` via the /tts API, cancelling any audio already in flight. */
export async function speak(text: string, onEnd?: () => void): Promise<void> {
  if (typeof window === "undefined") return;
  stopSpeech();
  try {
    const res = await fetch("/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text }),
    });
    if (!res.ok) {
      onEnd?.();
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.play().catch(() => {
      /* playback may be blocked until user gesture */
      onEnd?.();
    });
    currentAudio = audio;
    audio.onended = () => {
      if (currentAudio === audio) currentAudio = null;
      URL.revokeObjectURL(url);
      onEnd?.();
    };
    audio.onerror = () => {
      if (currentAudio === audio) currentAudio = null;
      URL.revokeObjectURL(url);
      onEnd?.();
    };
  } catch {
    /* best effort */
    onEnd?.();
  }
}

/** Cancel any in-flight speech. */
export function stopSpeech(): void {
  if (currentAudio) {
    try {
      currentAudio.pause();
      currentAudio.src = "";
    } catch {
      /* ignore */
    }
    currentAudio = null;
  }
}

export function isSpeaking(): boolean {
  return currentAudio !== null && !currentAudio.paused && !currentAudio.ended;
}

export function speechSynthesisSupported(): boolean {
  return typeof window !== "undefined";
}

export function ttsReady(): boolean {
  return typeof window !== "undefined";
}

export function ttsVoiceName(): string {
  return "melotts";
}

export function ttsFailureCount(): number {
  return 0;
}

export function ensureVoices(): void {
  /* API TTS has no voice inventory to load */
}
