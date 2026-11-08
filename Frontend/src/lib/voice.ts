/**
 * Voice I/O via Groq (same GROQ_API_KEY as Chief's model calls).
 *
 * STT:  POST /voice/stt   with multipart audio -> text
 * TTS:  POST /voice/tts   with JSON { text }     -> base64 WAV audio
 *
 * Replaces the Web Speech API so behaviour is identical on desktop and mobile
 * (Web Speech is unreliable on iOS).  The Worker proxies to Groq, so the
 * browser never sees the API key.
 */

export interface DictationHandle {
  stop: () => void;
}

let mediaRecorder: MediaRecorder | null = null;
let audioChunks: Blob[] = [];
let onFinalCallback: ((text: string) => void) | null = null;
let onEndCallback: (() => void) | null = null;

function isVoiceSupported(): boolean {
  return typeof window !== "undefined" && typeof MediaRecorder !== "undefined";
}

export function speechSupported(): boolean {
  return isVoiceSupported();
}

export function startDictation(opts: {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onEnd: () => void;
  onError?: (msg: string) => void;
}): DictationHandle | null {
  if (!isVoiceSupported()) {
    opts.onError?.("Voice input isn't supported on this device.");
    return null;
  }

  audioChunks = [];
  onFinalCallback = opts.onFinal;
  onEndCallback = opts.onEnd;

  navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((stream) => openRecorder(stream, opts))
    .catch(() => {
      opts.onError?.("Could not access microphone.");
      onEndCallback?.();
    });

  return {
    stop: () => {
      if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
      }
      mediaRecorder = null;
      onFinalCallback = null;
      onEndCallback = null;
    },
  };
}

function openRecorder(stream: MediaStream, opts: {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  onEnd: () => void;
  onError?: (msg: string) => void;
}): void {
  let rec: MediaRecorder | null = null;
  try {
    rec = new MediaRecorder(stream, { mimeType: "audio/webm" });
  } catch {
    try {
      rec = new MediaRecorder(stream);
    } catch {
      stream.getTracks().forEach((t) => t.stop());
      opts.onError?.("Could not start recorder.");
      return;
    }
  }

  mediaRecorder = rec;

  rec.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) audioChunks.push(e.data);
  };

  rec.onstop = async () => {
    const blob = new Blob(audioChunks, { type: "audio/webm" });
    audioChunks = [];
    stream.getTracks().forEach((t) => t.stop());
    mediaRecorder = null;
    try {
      const form = new FormData();
      form.append("audio", blob, "audio.webm");
      const resp = await fetch("/voice/stt", {
        method: "POST",
        body: form,
      });
      if (!resp.ok) {
        opts.onError?.("Voice transcription failed.");
        onEndCallback?.();
        return;
      }
      const data = await resp.json();
      if (data.ok && typeof data.text === "string") {
        onFinalCallback?.(data.text);
      } else {
        opts.onError?.("Could not transcribe speech.");
      }
    } catch {
      opts.onError?.("Voice transcription failed.");
    } finally {
      onEndCallback?.();
      onFinalCallback = null;
      onEndCallback = null;
    }
  };

  rec.onerror = () => {
    opts.onError?.("Microphone error.");
    stream.getTracks().forEach((t) => t.stop());
    mediaRecorder = null;
    onEndCallback?.();
  };

  try {
    rec.start(1000);
  } catch {
    stream.getTracks().forEach((t) => t.stop());
    mediaRecorder = null;
    opts.onError?.("Could not start recording.");
  }
}

/** Speak text aloud using Groq TTS via the Worker endpoint, with browser SpeechSynthesis fallback. */
export async function speakText(text: string): Promise<void> {
  if (!isVoiceSupported()) return;
  try {
    const resp = await fetch("/voice/tts", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data.ok && data.audioBase64) {
        const audio = new Audio(`data:${data.contentType ?? "audio/wav"};base64,${data.audioBase64}`);
        await new Promise<void>((resolve, reject) => {
          audio.onended = () => resolve();
          audio.onerror = () => reject(new Error("TTS playback error"));
          audio.play().catch(reject);
        });
        return;
      }
    }
  } catch {
    /* fall through to browser fallback */
  }
  // Fallback: browser SpeechSynthesis (only when Groq TTS isn't available yet).
  try {
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    await new Promise<void>((resolve) => {
      utterance.onend = () => resolve();
      utterance.onerror = () => resolve();
      window.speechSynthesis.speak(utterance);
    });
  } catch {
    /* silent -- voice is a nice-to-have */
  }
}
